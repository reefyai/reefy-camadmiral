import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from camadmiral.storage import CameraRepository
from camadmiral.media import ProbeResult


class CameraRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "camadmiral.db"
        self.repository = CameraRepository(self.database, b"k" * 32)
        self.repository.migrate()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_adoption_encrypts_password_and_preserves_stable_ids(self) -> None:
        candidate = {"candidate_uuid": "candidate-1", "display_name": "Test camera", "ip": "192.168.1.2"}
        profiles = [
            {"token": "main", "name": "Main", "uri": "rtsp://192.168.1.2/main", "width": 1920, "height": 1080, "encoding": "H264", "fps": 20, "bitrate_kbps": 4096},
            {"token": "sub", "name": "Sub", "uri": "rtsp://192.168.1.2/sub", "width": 640, "height": 360, "encoding": "H264", "fps": 10, "bitrate_kbps": 512},
        ]
        first = self.repository.adopt(candidate, "operator", "synthetic-secret", profiles, {"record": "main", "detect": "sub"})
        second = self.repository.adopt(candidate, "operator", "replacement-secret", profiles, {"record": "main", "detect": "sub"})

        self.assertEqual(first["camera_uuid"], second["camera_uuid"])
        self.assertEqual(first["roles"], second["roles"])
        self.assertEqual(second["role_tokens"], {"record": "main", "detect": "sub"})
        self.assertEqual(self.repository.credentials_for_candidate("candidate-1"), ("operator", "replacement-secret"))
        self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)
        with self.repository.connect() as connection:
            payload = connection.execute("SELECT password_ciphertext FROM camera_credentials").fetchone()[0]
            stream_ids = [row[0] for row in connection.execute("SELECT stream_uuid FROM managed_streams ORDER BY stream_uuid")]
        self.assertNotIn(b"replacement-secret", payload)
        self.assertEqual(len(stream_ids), 2)
        self.assertEqual({stream["probe_status"] for stream in second["streams"]}, {"pending"})

        self.repository.record_probe_results(
            {stream_ids[0]: ProbeResult("ready", 125, "h264", "aac", 1920, 1080, 20)}
        )
        refreshed = self.repository.adoption_for_candidate("candidate-1")
        self.assertIsNotNone(refreshed)
        self.assertIn("ready", {stream["probe_status"] for stream in refreshed["streams"]})

    def test_rtsp_access_password_is_stable_and_encrypted(self) -> None:
        first = self.repository.rtsp_access_password()
        second = CameraRepository(self.database, b"k" * 32).rtsp_access_password()

        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 24)
        with self.repository.connect() as connection:
            ciphertext = connection.execute(
                "SELECT secret_ciphertext FROM service_secrets WHERE name = 'rtsp-access'"
            ).fetchone()[0]
        self.assertNotIn(first.encode(), ciphertext)

    def test_frigate_targets_are_managed_in_sqlite(self) -> None:
        self.repository.save_frigate_target(
            "frigate-synthetic",
            "Synthetic Frigate",
            "http://127.0.0.1:20001",
            sync_cameras=True,
        )
        self.repository.record_frigate_target_check(
            "frigate-synthetic",
            status="connected",
        )

        target = self.repository.frigate_target("frigate-synthetic")
        self.assertEqual(target["api_url"], "http://127.0.0.1:20001")
        self.assertTrue(target["sync_cameras"])
        self.assertEqual(target["connection_status"], "connected")
        self.assertEqual(
            [item["target_id"] for item in self.repository.frigate_targets(sync_only=True)],
            ["frigate-synthetic"],
        )

        self.repository.save_frigate_target(
            "frigate-synthetic",
            "Synthetic Frigate",
            "http://127.0.0.1:20001",
            sync_cameras=False,
        )
        self.assertEqual(self.repository.frigate_targets(sync_only=True), [])
        self.assertTrue(self.repository.remove_frigate_target("frigate-synthetic"))
        self.assertIsNone(self.repository.frigate_target("frigate-synthetic"))

    def test_incident_lifecycle_deduplicates_outage_and_notifies_recovery(self) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-incident", "display_name": "Synthetic entrance"},
            "operator",
            "synthetic-secret",
            [{
                "token": "stream", "name": "Stream", "uri": "rtsp://192.0.2.60/live",
                "width": 1280, "height": 720, "encoding": "H264", "fps": 15,
                "bitrate_kbps": 0,
            }],
            {"record": "stream", "detect": "stream"},
        )
        stream_uuid = adoption["streams"][0]["stream_uuid"]
        self.repository.save_telegram_settings(
            enabled=True,
            bot_token="123456:synthetic-bot-token-value",
            bot_id="123456",
            bot_username="synthetic_alert_bot",
            pairing_token="synthetic-pairing-token",
            pairing_expires_at="2099-01-01T00:00:00+00:00",
        )
        self.repository.complete_telegram_pairing(
            chat_id="100200300",
            chat_label="Synthetic operator",
            update_offset=7,
        )

        self.repository.record_probe_results({stream_uuid: ProbeResult("ready", 10)})
        for _ in range(3):
            self.repository.record_probe_results({stream_uuid: ProbeResult("unavailable", 10)})
        first = self.repository.incidents(status="open")
        self.assertEqual(first["open_count"], 1)
        self.assertEqual(first["incidents"][0]["kind"], "media_offline")

        self.repository.record_probe_results({stream_uuid: ProbeResult("unavailable", 10)})
        repeated = self.repository.incidents(status="open")
        self.assertEqual(len(repeated["incidents"]), 1)

        self.repository.record_probe_results({stream_uuid: ProbeResult("ready", 10)})
        resolved = self.repository.incidents(status="resolved")
        self.assertEqual(resolved["open_count"], 0)
        self.assertEqual(resolved["incidents"][0]["resolution_reason"], "recovered")
        notifications = self.repository.due_notifications()
        self.assertEqual(
            [item["event_type"] for item in notifications],
            ["incident_opened", "incident_resolved"],
        )
        self.assertNotIn("192.0.2.60", str(notifications))
        self.assertNotIn("synthetic-secret", str(notifications))

    def test_telegram_token_and_pairing_secret_are_encrypted_and_never_exposed(self) -> None:
        bot_token = "123456:synthetic-bot-token-value"
        pairing_token = "synthetic-pairing-token"
        self.repository.save_telegram_settings(
            enabled=True,
            bot_token=bot_token,
            bot_id="123456",
            bot_username="synthetic_alert_bot",
            pairing_token=pairing_token,
            pairing_expires_at="2099-01-01T00:00:00+00:00",
        )

        public = self.repository.notification_settings()
        credentials = self.repository.notification_credentials()
        self.assertNotIn("token", str(public))
        self.assertEqual(credentials["bot_token"], bot_token)
        self.assertEqual(credentials["pairing_token"], pairing_token)
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT bot_token_ciphertext, pairing_token_ciphertext FROM notification_settings"
            ).fetchone()
        self.assertNotIn(bot_token.encode(), row[0])
        self.assertNotIn(pairing_token.encode(), row[1])

    def test_migration_enables_alerts_for_an_existing_configured_bot(self) -> None:
        self.repository.save_telegram_settings(
            enabled=False,
            bot_token="123456:synthetic-bot-token-value",
            bot_id="123456",
            bot_username="synthetic_alert_bot",
        )
        with self.repository.connect() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version = 12")
            connection.commit()

        self.repository.migrate()

        self.assertTrue(self.repository.notification_settings()["enabled"])

    def test_frigate_binding_tracks_pending_applied_and_error_without_secrets(self) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-frigate", "display_name": "Camera"},
            "operator",
            "upstream-secret",
            [
                {
                    "token": "stream",
                    "name": "Stream",
                    "uri": "rtsp://192.0.2.20/live",
                    "width": 1280,
                    "height": 720,
                    "encoding": "H264",
                    "fps": 15,
                    "bitrate_kbps": 0,
                }
            ],
            {"record": "stream", "detect": "stream"},
        )
        stream_uuid = adoption["streams"][0]["stream_uuid"]
        self.repository.record_frigate_attempt(
            "frigate-primary",
            adoption["camera_uuid"],
            "camadmiral_synthetic",
            stream_uuid,
            stream_uuid,
            "desired-hash",
        )
        self.repository.complete_frigate_attempt(
            "frigate-primary",
            adoption["camera_uuid"],
            status="applied",
            applied_hash="desired-hash",
        )

        binding = self.repository.frigate_binding("frigate-primary", adoption["camera_uuid"])
        self.assertEqual(binding["status"], "applied")
        self.assertEqual(binding["applied_hash"], "desired-hash")
        self.assertNotIn("upstream-secret", str(binding))

        self.repository.record_frigate_attempt(
            "frigate-primary",
            adoption["camera_uuid"],
            "camadmiral_synthetic",
            stream_uuid,
            stream_uuid,
            "new-hash",
        )
        self.repository.complete_frigate_attempt(
            "frigate-primary",
            adoption["camera_uuid"],
            status="error",
            error_code="target_unavailable",
        )
        binding = self.repository.frigate_binding("frigate-primary", adoption["camera_uuid"])
        self.assertEqual(binding["status"], "error")
        self.assertEqual(binding["last_error_code"], "target_unavailable")
        self.assertEqual(binding["applied_hash"], "desired-hash")

    def test_preview_prefers_healthy_detection_stream(self) -> None:
        candidate = {"candidate_uuid": "candidate-preview", "display_name": "Camera"}
        adoption = self.repository.adopt(
            candidate,
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "main",
                    "name": "Main",
                    "uri": "rtsp://192.168.1.20/main",
                    "width": 1920,
                    "height": 1080,
                    "encoding": "H264",
                    "fps": 20,
                    "bitrate_kbps": 2048,
                },
                {
                    "token": "sub",
                    "name": "Sub",
                    "uri": "rtsp://192.168.1.20/sub",
                    "width": 640,
                    "height": 360,
                    "encoding": "H264",
                    "fps": 10,
                    "bitrate_kbps": 256,
                },
            ],
            {"record": "main", "detect": "sub"},
        )
        by_token = {stream["profile_token"]: stream for stream in adoption["streams"]}
        self.repository.record_probe_results(
            {
                by_token["main"]["stream_uuid"]: ProbeResult("ready", 20),
                by_token["sub"]["stream_uuid"]: ProbeResult("ready", 20),
            }
        )

        selected = self.repository.preview_stream_for_camera(adoption["camera_uuid"])

        self.assertEqual(selected["stream_uuid"], by_token["sub"]["stream_uuid"])
        self.assertNotIn("uri", selected)
        self.assertNotIn("password", selected)
        self.assertIsNone(self.repository.preview_stream_for_camera("camera-missing"))

    def test_disable_preserves_camera_but_withdraws_active_media(self) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-disabled", "display_name": "Side entrance"},
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "stream",
                    "name": "Stream",
                    "uri": "rtsp://192.0.2.30/live",
                    "width": 1280,
                    "height": 720,
                    "encoding": "H264",
                    "fps": 15,
                    "bitrate_kbps": 0,
                }
            ],
            {"record": "stream", "detect": "stream"},
        )

        self.assertTrue(adoption["enabled"])
        self.assertTrue(self.repository.set_camera_enabled(adoption["camera_uuid"], False))

        disabled = self.repository.adoption_for_candidate("candidate-disabled")
        self.assertFalse(disabled["enabled"])
        self.assertEqual(len(disabled["streams"]), 1)
        self.assertEqual(self.repository.managed_stream_sources(), [])
        self.assertIsNone(self.repository.preview_stream_for_camera(adoption["camera_uuid"]))
        saved = self.repository.managed_stream_sources(
            include_disabled=True,
            camera_uuid=adoption["camera_uuid"],
            role_bound_only=True,
        )
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["password"], "synthetic-secret")

    def test_camera_name_update_preserves_stable_identity(self) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-rename", "display_name": "Old name"},
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "stream",
                    "name": "Stream",
                    "uri": "rtsp://192.0.2.40/live",
                    "width": 640,
                    "height": 360,
                    "encoding": "H264",
                    "fps": 10,
                    "bitrate_kbps": 0,
                }
            ],
            {"record": "stream", "detect": "stream"},
        )

        self.assertTrue(self.repository.update_camera_name(adoption["camera_uuid"], "New name"))
        renamed = self.repository.adoption_for_candidate("candidate-rename")
        self.assertEqual(renamed["camera_uuid"], adoption["camera_uuid"])
        self.assertEqual(renamed["display_name"], "New name")

    def test_credential_replacement_preserves_ids_and_releases_auth_failure(self) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-credential", "display_name": "Camera"},
            "old-user",
            "old-secret",
            [
                {
                    "token": "stream",
                    "name": "Stream",
                    "uri": "rtsp://192.0.2.50/live",
                    "width": 1280,
                    "height": 720,
                    "encoding": "H264",
                    "fps": 15,
                    "bitrate_kbps": 0,
                }
            ],
            {"record": "stream", "detect": "stream"},
        )
        stream_uuid = adoption["streams"][0]["stream_uuid"]
        self.repository.record_camera_auth_failure(
            adoption["camera_uuid"], ProbeResult("auth_failed", 20)
        )
        self.assertEqual(
            self.repository.managed_stream_sources(include_auth_failed=False),
            [],
        )

        replaced = self.repository.replace_camera_credentials(
            adoption["camera_uuid"],
            "new-user",
            "new-secret",
        )

        self.assertTrue(replaced)
        refreshed = self.repository.adoption_for_candidate("candidate-credential")
        self.assertEqual(refreshed["camera_uuid"], adoption["camera_uuid"])
        self.assertEqual(refreshed["streams"][0]["stream_uuid"], stream_uuid)
        self.assertEqual(refreshed["streams"][0]["health_status"], "unknown")
        self.assertEqual(
            self.repository.credentials_for_candidate("candidate-credential"),
            ("new-user", "new-secret"),
        )
        self.assertEqual(
            len(self.repository.managed_stream_sources(include_auth_failed=False)),
            1,
        )

    def test_consumer_inventory_excludes_upstream_urls_and_credentials(self) -> None:
        candidate = {"candidate_uuid": "candidate-consumer", "display_name": "Entrance"}
        adoption = self.repository.adopt(
            candidate,
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "main",
                    "name": "Main",
                    "uri": "rtsp://192.0.2.20/private-source",
                    "width": 1920,
                    "height": 1080,
                    "encoding": "H264",
                    "fps": 20,
                    "bitrate_kbps": 2048,
                }
            ],
            {"record": "main", "detect": "main"},
        )

        inventory = self.repository.consumer_inventory()

        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["camera_uuid"], adoption["camera_uuid"])
        self.assertEqual(inventory[0]["streams"][0]["roles"], ["detect", "record"])
        serialized = str(inventory)
        self.assertNotIn("private-source", serialized)
        self.assertNotIn("synthetic-secret", serialized)
        self.assertNotIn("operator", serialized)

    def test_manual_rtsp_source_is_structured_and_reconciled_like_onvif(self) -> None:
        candidate = {
            "candidate_uuid": "candidate-rtsp",
            "display_name": "Legacy camera",
            "ip": "192.168.1.8",
        }
        profiles = [
            {
                "token": "manual-synthetic",
                "name": "Stream 1",
                "uri": "rtsp://192.168.1.8:8554/live?channel=1",
                "width": 1280,
                "height": 720,
                "encoding": "H264",
                "fps": 15,
                "bitrate_kbps": 0,
                "source_kind": "manual_rtsp",
            }
        ]

        adoption = self.repository.adopt(
            candidate,
            "operator",
            "synthetic-secret",
            profiles,
            {"record": "manual-synthetic", "detect": "manual-synthetic"},
        )
        sources = self.repository.managed_stream_sources()

        self.assertEqual(adoption["streams"][0]["source_kind"], "manual_rtsp")
        self.assertEqual(sources[0]["uri"], "rtsp://192.168.1.8:8554/live?channel=1")
        self.assertEqual(sources[0]["username"], "operator")
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT source_scheme, source_host, source_port, source_path, source_query "
                "FROM onvif_profiles WHERE profile_token = 'manual-synthetic'"
            ).fetchone()
        self.assertEqual(tuple(row), ("rtsp", "192.168.1.8", 8554, "/live", "channel=1"))

    def test_health_progresses_from_healthy_to_degraded_to_offline(self) -> None:
        candidate = {"candidate_uuid": "candidate-health", "display_name": "Camera"}
        profiles = [
            {
                "token": "stream",
                "name": "Stream",
                "uri": "rtsp://192.168.1.9/live",
                "width": 1280,
                "height": 720,
                "encoding": "H264",
                "fps": 15,
                "bitrate_kbps": 0,
            }
        ]
        adoption = self.repository.adopt(
            candidate,
            "operator",
            "synthetic-secret",
            profiles,
            {"record": "stream", "detect": "stream"},
        )
        stream_uuid = adoption["streams"][0]["stream_uuid"]

        self.repository.record_probe_results(
            {stream_uuid: ProbeResult("ready", 25, "h264", None, 1280, 720, 15)}
        )
        self.repository.record_probe_results({stream_uuid: ProbeResult("unavailable", 30)})
        grace = self.repository.adoption_for_candidate("candidate-health")
        self.assertEqual(grace["streams"][0]["health_status"], "healthy")

        self.repository.record_probe_results({stream_uuid: ProbeResult("unavailable", 30)})
        degraded = self.repository.adoption_for_candidate("candidate-health")
        self.assertEqual(degraded["streams"][0]["health_status"], "degraded")
        self.assertEqual(degraded["streams"][0]["probed_width"], 1280)

        self.repository.record_probe_results({stream_uuid: ProbeResult("unavailable", 30)})
        offline = self.repository.adoption_for_candidate("candidate-health")
        self.assertEqual(offline["streams"][0]["health_status"], "offline")
        self.assertEqual(offline["streams"][0]["consecutive_failures"], 3)

    def test_health_history_records_transitions_without_duplicate_poll_events(self) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-history", "display_name": "Camera"},
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "stream",
                    "name": "Stream",
                    "uri": "rtsp://192.0.2.40/live",
                    "width": 1280,
                    "height": 720,
                    "encoding": "H264",
                    "fps": 15,
                    "bitrate_kbps": 0,
                }
            ],
            {"record": "stream", "detect": "stream"},
        )
        stream_uuid = adoption["streams"][0]["stream_uuid"]

        self.repository.record_probe_results({stream_uuid: ProbeResult("ready", 20)})
        self.repository.record_probe_results({stream_uuid: ProbeResult("unavailable", 20)})
        self.repository.record_probe_results({stream_uuid: ProbeResult("unavailable", 20)})
        self.repository.record_probe_results({stream_uuid: ProbeResult("unavailable", 20)})

        with self.repository.connect() as connection:
            states = [
                row["state"]
                for row in connection.execute(
                    "SELECT state FROM camera_health_events WHERE camera_uuid = ? ORDER BY event_id",
                    (adoption["camera_uuid"],),
                )
            ]
        self.assertEqual(states, ["unknown", "healthy", "degraded", "offline"])

    def test_unused_profile_failure_does_not_degrade_camera_health(self) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-unused", "display_name": "Camera"},
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "selected", "name": "Selected", "uri": "rtsp://192.0.2.42/main",
                    "width": 1280, "height": 720, "encoding": "H264", "fps": 15,
                    "bitrate_kbps": 0,
                },
                {
                    "token": "unused", "name": "Unused", "uri": "rtsp://192.0.2.42/extra",
                    "width": 640, "height": 360, "encoding": "H264", "fps": 10,
                    "bitrate_kbps": 0,
                },
            ],
            {"record": "selected", "detect": "selected"},
        )
        streams = {stream["profile_token"]: stream["stream_uuid"] for stream in adoption["streams"]}
        self.repository.record_probe_results({
            streams["selected"]: ProbeResult("ready", 20),
            streams["unused"]: ProbeResult("ready", 20),
        })
        for _ in range(3):
            self.repository.record_probe_results({streams["unused"]: ProbeResult("unavailable", 20)})

        refreshed = self.repository.adoption_for_candidate("candidate-unused")
        states = {stream["profile_token"]: stream["health_status"] for stream in refreshed["streams"]}
        self.assertEqual(states, {"selected": "healthy", "unused": "offline"})
        with self.repository.connect() as connection:
            camera_state = connection.execute(
                "SELECT state FROM camera_health_events WHERE camera_uuid = ? "
                "ORDER BY event_id DESC LIMIT 1",
                (adoption["camera_uuid"],),
            ).fetchone()["state"]
        self.assertEqual(camera_state, "healthy")

    def test_idle_relay_is_unobserved_instead_of_degraded(self) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-idle", "display_name": "Camera"},
            "operator",
            "synthetic-secret",
            [{
                "token": "stream", "name": "Stream", "uri": "rtsp://192.0.2.43/live",
                "width": 1280, "height": 720, "encoding": "H264", "fps": 15,
                "bitrate_kbps": 0,
            }],
            {"record": "stream", "detect": "stream"},
        )
        stream_uuid = adoption["streams"][0]["stream_uuid"]
        self.repository.record_probe_results({stream_uuid: ProbeResult("ready", 20)})
        self.repository.record_probe_results({stream_uuid: ProbeResult("idle", 1)})

        stream = self.repository.adoption_for_candidate("candidate-idle")["streams"][0]
        self.assertEqual(stream["probe_status"], "idle")
        self.assertEqual(stream["health_status"], "unknown")
        self.assertEqual(stream["consecutive_failures"], 0)

    def test_active_healthy_role_keeps_camera_healthy_when_another_role_is_idle(self) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-partly-idle", "display_name": "Camera"},
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "record", "name": "Record", "uri": "rtsp://192.0.2.44/main",
                    "width": 1920, "height": 1080, "encoding": "H264", "fps": 20,
                    "bitrate_kbps": 0,
                },
                {
                    "token": "detect", "name": "Detect", "uri": "rtsp://192.0.2.44/sub",
                    "width": 640, "height": 360, "encoding": "H264", "fps": 10,
                    "bitrate_kbps": 0,
                },
            ],
            {"record": "record", "detect": "detect"},
        )
        streams = {stream["profile_token"]: stream["stream_uuid"] for stream in adoption["streams"]}
        self.repository.record_probe_results({
            streams["record"]: ProbeResult("idle", 1),
            streams["detect"]: ProbeResult("ready", 1),
        })

        with self.repository.connect() as connection:
            state = connection.execute(
                "SELECT state FROM camera_health_events WHERE camera_uuid = ? "
                "ORDER BY event_id DESC LIMIT 1",
                (adoption["camera_uuid"],),
            ).fetchone()["state"]
        self.assertEqual(state, "healthy")

    def test_availability_excludes_unknown_and_disabled_time(self) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-availability", "display_name": "Camera"},
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "stream",
                    "name": "Stream",
                    "uri": "rtsp://192.0.2.41/live",
                    "width": 1280,
                    "height": 720,
                    "encoding": "H264",
                    "fps": 15,
                    "bitrate_kbps": 0,
                }
            ],
            {"record": "stream", "detect": "stream"},
        )
        now = datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc)
        events = [
            ("healthy", "all_streams_healthy", now - timedelta(hours=12)),
            ("degraded", "media_probe_failed", now - timedelta(hours=6)),
            ("disabled", "operator_disabled", now - timedelta(hours=3)),
        ]
        with self.repository.connect() as connection:
            connection.execute(
                "DELETE FROM camera_health_events WHERE camera_uuid = ?",
                (adoption["camera_uuid"],),
            )
            connection.executemany(
                "INSERT INTO camera_health_events(camera_uuid, state, reason, observed_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    (adoption["camera_uuid"], state, reason, observed_at.isoformat())
                    for state, reason, observed_at in events
                ],
            )
            connection.commit()

        timeline = self.repository.camera_availability(
            adoption["camera_uuid"],
            hours=24,
            bucket_count=4,
            now=now,
        )

        self.assertEqual(timeline["availability_percent"], 66.67)
        self.assertEqual(timeline["observed_seconds"], 9 * 60 * 60)
        self.assertEqual(
            [bucket["state"] for bucket in timeline["buckets"]],
            ["unknown", "unknown", "healthy", "disabled"],
        )

    def test_availability_bucket_preserves_recovery_transition(self) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-recovery-timeline", "display_name": "Camera"},
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "stream",
                    "name": "Stream",
                    "uri": "rtsp://192.0.2.42/live",
                    "width": 1280,
                    "height": 720,
                    "encoding": "H264",
                    "fps": 15,
                    "bitrate_kbps": 0,
                }
            ],
            {"record": "stream", "detect": "stream"},
        )
        now = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        with self.repository.connect() as connection:
            connection.execute(
                "DELETE FROM camera_health_events WHERE camera_uuid = ?",
                (adoption["camera_uuid"],),
            )
            connection.executemany(
                "INSERT INTO camera_health_events(camera_uuid, state, reason, observed_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    (
                        adoption["camera_uuid"],
                        "offline",
                        "all_streams_offline",
                        (now - timedelta(minutes=30)).isoformat(),
                    ),
                    (
                        adoption["camera_uuid"],
                        "healthy",
                        "all_streams_healthy",
                        (now - timedelta(minutes=20)).isoformat(),
                    ),
                ],
            )
            connection.commit()

        timeline = self.repository.camera_availability(
            adoption["camera_uuid"],
            hours=1,
            bucket_count=2,
            now=now,
        )

        recovery_bucket = timeline["buckets"][-1]
        self.assertEqual(recovery_bucket["state"], "healthy")
        self.assertEqual(recovery_bucket["reason"], "all_streams_healthy")
        self.assertEqual(
            [(segment["state"], segment["seconds"]) for segment in recovery_bucket["segments"]],
            [("offline", 600.0), ("healthy", 1200.0)],
        )

    def test_address_recovery_preserves_stream_identity_and_records_event(self) -> None:
        candidate = {"candidate_uuid": "candidate-move", "display_name": "Camera"}
        profiles = [
            {
                "token": "stream",
                "name": "Stream",
                "uri": "rtsp://192.168.1.20/live",
                "width": 1280,
                "height": 720,
                "encoding": "H264",
                "fps": 15,
                "bitrate_kbps": 0,
            }
        ]
        before = self.repository.adopt(
            candidate,
            "operator",
            "synthetic-secret",
            profiles,
            {"record": "stream", "detect": "stream"},
        )

        self.repository.update_profile_sources(
            before["camera_uuid"],
            {"stream": "rtsp://192.168.1.99/live"},
        )
        self.repository.record_address_change(
            before["camera_uuid"],
            "192.168.1.20",
            "192.168.1.99",
            "unique-mac",
        )
        after = self.repository.adoption_for_candidate("candidate-move")

        self.assertEqual(after["streams"][0]["stream_uuid"], before["streams"][0]["stream_uuid"])
        self.assertEqual(after["streams"][0]["stream_key"], before["streams"][0]["stream_key"])
        self.assertEqual(after["streams"][0]["uri"], "rtsp://192.168.1.99/live")
        with self.repository.connect() as connection:
            event = connection.execute(
                "SELECT previous_address, current_address, evidence FROM camera_address_events"
            ).fetchone()
        self.assertEqual(tuple(event), ("192.168.1.20", "192.168.1.99", "unique-mac"))

    def test_media_revisions_are_secret_free_and_keep_last_known_good(self) -> None:
        candidate = {"candidate_uuid": "candidate-revision", "display_name": "Camera"}
        self.repository.adopt(
            candidate,
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "stream",
                    "name": "Stream",
                    "uri": "rtsp://192.168.1.20/live",
                    "width": 1280,
                    "height": 720,
                    "encoding": "H264",
                    "fps": 15,
                    "bitrate_kbps": 0,
                }
            ],
            {"record": "stream", "detect": "stream"},
        )
        sources = self.repository.managed_stream_sources()

        applied_id, state = self.repository.record_desired_media_revision(sources)
        self.assertEqual(state, "desired")
        self.repository.complete_media_revision(applied_id, "applied")
        self.repository.update_profile_sources(
            self.repository.adoption_for_candidate("candidate-revision")["camera_uuid"],
            {"stream": "rtsp://192.168.1.99/live"},
        )
        failed_id, failed_state = self.repository.record_desired_media_revision(
            self.repository.managed_stream_sources()
        )
        self.assertEqual(failed_state, "desired")
        self.repository.complete_media_revision(failed_id, "failed", "runtime_failed")

        last_good = self.repository.last_known_good_media_revision()
        self.assertEqual(last_good["revision_id"], applied_id)
        self.assertEqual(last_good["config"]["streams"][0]["source"]["host"], "192.168.1.20")
        with self.repository.connect() as connection:
            stored = "\n".join(
                row["config_json"]
                for row in connection.execute("SELECT config_json FROM media_config_revisions")
            )
        self.assertNotIn("operator", stored)
        self.assertNotIn("synthetic-secret", stored)

    def test_auth_failed_sources_are_suppressed_until_credentials_change(self) -> None:
        candidate = {"candidate_uuid": "candidate-auth", "display_name": "Camera"}
        profiles = [
            {
                "token": "stream",
                "name": "Stream",
                "uri": "rtsp://192.168.1.10/live",
                "width": 640,
                "height": 360,
                "encoding": "H264",
                "fps": 10,
                "bitrate_kbps": 0,
            }
        ]
        adoption = self.repository.adopt(
            candidate,
            "operator",
            "wrong-secret",
            profiles,
            {"record": "stream", "detect": "stream"},
        )
        stream_uuid = adoption["streams"][0]["stream_uuid"]

        self.repository.record_camera_auth_failure(
            adoption["camera_uuid"], ProbeResult("auth_failed", 20)
        )

        self.assertEqual(self.repository.managed_stream_sources(include_auth_failed=False), [])
        failed = self.repository.adoption_for_candidate("candidate-auth")
        self.assertEqual(failed["streams"][0]["health_status"], "auth_failed")

        self.repository.adopt(
            candidate,
            "operator",
            "replacement-secret",
            profiles,
            {"record": "stream", "detect": "stream"},
        )
        self.assertEqual(len(self.repository.managed_stream_sources(include_auth_failed=False)), 1)


if __name__ == "__main__":
    unittest.main()
