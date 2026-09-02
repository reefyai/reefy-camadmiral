import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from camadmiral.storage import MIGRATIONS, CameraRepository
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
        self.assertEqual(second["stream_address_mode"], "lan")
        self.assertTrue(
            self.repository.set_camera_stream_address_mode(
                second["camera_uuid"], "localhost"
            )
        )
        self.assertEqual(
            self.repository.adoption_for_candidate("candidate-1")["stream_address_mode"],
            "localhost",
        )
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

    def test_discovery_network_settings_persist_custom_and_excluded_subnets(self) -> None:
        self.assertEqual(
            self.repository.discovery_network_settings(),
            {
                "custom_subnets": [],
                "excluded_detected_subnets": [],
                "excluded_custom_subnets": [],
            },
        )

        self.repository.save_discovery_network_settings(
            custom_subnets=["10.0.202.0/24"],
            excluded_detected_subnets=["192.168.40.0/24"],
            excluded_custom_subnets=["10.0.202.0/24"],
        )

        self.assertEqual(
            CameraRepository(self.database, b"k" * 32).discovery_network_settings(),
            {
                "custom_subnets": ["10.0.202.0/24"],
                "excluded_detected_subnets": ["192.168.40.0/24"],
                "excluded_custom_subnets": ["10.0.202.0/24"],
            },
        )

    def test_discovery_checkbox_migration_preserves_existing_custom_subnets(self) -> None:
        with self.repository.connect() as connection:
            connection.execute("DROP TABLE discovery_settings")
            connection.execute(
                "CREATE TABLE discovery_settings ("
                "singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1), "
                "custom_subnets_json TEXT NOT NULL DEFAULT '[]', "
                "excluded_detected_subnets_json TEXT NOT NULL DEFAULT '[]', "
                "updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO discovery_settings VALUES (1, '[\"10.0.202.0/24\"]', '[]', 'now')"
            )
            connection.execute(
                "DELETE FROM schema_migrations WHERE version = ?", (18,)
            )
            connection.commit()

        self.repository.migrate()

        self.assertEqual(
            self.repository.discovery_network_settings(),
            {
                "custom_subnets": ["10.0.202.0/24"],
                "excluded_detected_subnets": [],
                "excluded_custom_subnets": [],
            },
        )

    def test_frigate_targets_are_managed_in_sqlite(self) -> None:
        self.repository.save_frigate_target(
            "frigate-synthetic",
            "Synthetic Frigate",
            "http://127.0.0.1:20001",
        )
        self.repository.record_frigate_target_check(
            "frigate-synthetic",
            status="connected",
        )

        target = self.repository.frigate_target("frigate-synthetic")
        self.assertEqual(target["api_url"], "http://127.0.0.1:20001")
        self.assertNotIn("sync_cameras", target)
        self.assertEqual(target["selected_cameras"], 0)
        self.assertEqual(target["connection_status"], "connected")
        self.assertFalse(target["restart_recommended"])
        self.assertEqual(target["address_mode"], "lan")

        self.repository.record_frigate_target_check(
            "frigate-synthetic",
            status="connected",
            restart_recommended=True,
        )
        self.assertTrue(
            self.repository.frigate_target("frigate-synthetic")["restart_recommended"]
        )

        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-frigate", "display_name": "Synthetic camera"},
            "operator",
            "synthetic-secret",
            [{
                "token": "main", "name": "Main", "uri": "rtsp://192.0.2.30/main",
                "width": 1280, "height": 720, "encoding": "H264", "fps": 15,
                "bitrate_kbps": 0,
            }],
            {"record": "main", "detect": "main"},
        )
        camera_uuid = adoption["camera_uuid"]
        self.assertTrue(self.repository.select_frigate_camera("frigate-synthetic", camera_uuid))
        self.assertFalse(self.repository.select_frigate_camera("frigate-synthetic", camera_uuid))
        self.assertEqual(
            self.repository.frigate_camera_selections("frigate-synthetic"),
            [{"camera_uuid": camera_uuid, "address_mode": "lan"}],
        )
        self.assertTrue(
            self.repository.set_frigate_target_address_mode(
                "frigate-synthetic", "localhost"
            )
        )
        self.assertEqual(
            self.repository.frigate_camera_address_mode("frigate-synthetic", camera_uuid),
            "localhost",
        )
        self.assertFalse(
            self.repository.select_frigate_camera(
                "frigate-synthetic", camera_uuid, "localhost"
            )
        )
        self.assertEqual(
            self.repository.selected_frigate_camera_uuids("frigate-synthetic"),
            [camera_uuid],
        )
        self.assertEqual(self.repository.frigate_target("frigate-synthetic")["selected_cameras"], 1)
        self.assertTrue(self.repository.deselect_frigate_camera("frigate-synthetic", camera_uuid))
        self.assertEqual(self.repository.selected_frigate_camera_uuids("frigate-synthetic"), [])
        self.assertTrue(self.repository.remove_frigate_target("frigate-synthetic"))
        self.assertIsNone(self.repository.frigate_target("frigate-synthetic"))

    def test_frigate_selection_migration_does_not_backfill_existing_targets(self) -> None:
        self.repository.save_frigate_target(
            "frigate-existing",
            "Existing Frigate",
            "http://127.0.0.1:20002",
        )
        with self.repository.connect() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version = 14")
            connection.execute("DROP TABLE frigate_camera_selections")
            connection.execute(
                "UPDATE frigate_targets SET sync_cameras = 1 WHERE target_id = 'frigate-existing'"
            )
            connection.commit()

        self.repository.migrate()

        self.assertEqual(self.repository.selected_frigate_camera_uuids("frigate-existing"), [])

    def test_frigate_address_mode_migration_defaults_existing_selections_to_lan(self) -> None:
        self.repository.save_frigate_target(
            "frigate-existing",
            "Existing Frigate",
            "http://127.0.0.1:20002",
        )
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-existing", "display_name": "Synthetic camera"},
            "operator",
            "synthetic-secret",
            [{
                "token": "main", "name": "Main", "uri": "rtsp://192.0.2.30/main",
                "width": 1280, "height": 720, "encoding": "H264", "fps": 15,
                "bitrate_kbps": 0,
            }],
            {"record": "main", "detect": "main"},
        )
        camera_uuid = adoption["camera_uuid"]
        self.repository.select_frigate_camera("frigate-existing", camera_uuid)
        with self.repository.connect() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version = 16")
            connection.execute(
                "CREATE TABLE old_selections AS "
                "SELECT target_id, camera_uuid, selected_at FROM frigate_camera_selections"
            )
            connection.execute("DROP TABLE frigate_camera_selections")
            connection.execute(
                "CREATE TABLE frigate_camera_selections ("
                "target_id TEXT NOT NULL REFERENCES frigate_targets(target_id) ON DELETE CASCADE, "
                "camera_uuid TEXT NOT NULL REFERENCES cameras(camera_uuid) ON DELETE CASCADE, "
                "selected_at TEXT NOT NULL, PRIMARY KEY(target_id, camera_uuid))"
            )
            connection.execute(
                "INSERT INTO frigate_camera_selections SELECT * FROM old_selections"
            )
            connection.execute("DROP TABLE old_selections")
            connection.commit()

        self.repository.migrate()

        self.assertEqual(
            self.repository.frigate_camera_address_mode("frigate-existing", camera_uuid),
            "lan",
        )

    def test_stream_address_mode_migration_preserves_latest_frigate_choice(self) -> None:
        self.repository.save_frigate_target(
            "frigate-existing",
            "Existing Frigate",
            "http://127.0.0.1:20002",
        )
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-existing", "display_name": "Synthetic camera"},
            "operator",
            "synthetic-secret",
            [{
                "token": "main", "name": "Main", "uri": "rtsp://192.0.2.30/main",
                "width": 1280, "height": 720, "encoding": "H264", "fps": 15,
                "bitrate_kbps": 0,
            }],
            {"record": "main", "detect": "main"},
        )
        camera_uuid = adoption["camera_uuid"]
        self.repository.set_frigate_target_address_mode(
            "frigate-existing", "localhost"
        )
        self.repository.select_frigate_camera(
            "frigate-existing", camera_uuid, "localhost"
        )
        with self.repository.connect() as connection:
            connection.execute(
                "DELETE FROM schema_migrations WHERE version = ?", (19,)
            )
            connection.execute("ALTER TABLE cameras DROP COLUMN stream_address_mode")
            connection.commit()

        self.repository.migrate()

        self.assertEqual(
            self.repository.adoption_for_candidate("candidate-existing")["stream_address_mode"],
            "localhost",
        )

    def test_target_address_migration_preserves_mixed_legacy_selections(self) -> None:
        self.repository.save_frigate_target(
            "frigate-mixed",
            "Mixed Frigate",
            "http://127.0.0.1:20003",
        )
        camera_uuids = []
        for suffix in ("one", "two"):
            adoption = self.repository.adopt(
                {
                    "candidate_uuid": f"candidate-{suffix}",
                    "display_name": f"Synthetic {suffix}",
                },
                "operator",
                "synthetic-secret",
                [{
                    "token": "main",
                    "name": "Main",
                    "uri": f"rtsp://192.0.2.30/{suffix}",
                    "width": 1280,
                    "height": 720,
                    "encoding": "H264",
                    "fps": 15,
                    "bitrate_kbps": 0,
                }],
                {"record": "main", "detect": "main"},
            )
            camera_uuids.append(adoption["camera_uuid"])
            self.repository.select_frigate_camera(
                "frigate-mixed", adoption["camera_uuid"]
            )
        with self.repository.connect() as connection:
            connection.execute(
                "UPDATE frigate_camera_selections SET address_mode = 'localhost' "
                "WHERE target_id = ? AND camera_uuid = ?",
                ("frigate-mixed", camera_uuids[1]),
            )
            connection.execute("DELETE FROM schema_migrations WHERE version = 21")
            connection.execute("ALTER TABLE frigate_targets DROP COLUMN address_mode")
            connection.commit()

        self.repository.migrate()

        self.assertIsNone(
            self.repository.frigate_target("frigate-mixed")["address_mode"]
        )
        self.assertEqual(
            {
                selection["address_mode"]
                for selection in self.repository.frigate_camera_selections(
                    "frigate-mixed"
                )
            },
            {"lan", "localhost"},
        )

    def test_blocked_device_matches_only_stable_onvif_identity_or_mac(self) -> None:
        candidate = {
            "candidate_uuid": "candidate-blocked",
            "display_name": "Synthetic false positive",
            "ip": "192.0.2.20",
            "mac": "02:00:00:00:00:20",
            "onvif": {"endpoint_reference": "URN:UUID:SYNTHETIC-20"},
        }
        blocked = self.repository.block_candidate(candidate)

        self.assertEqual(blocked["onvif_identity"], "urn:uuid:synthetic-20")
        self.assertEqual(blocked["mac"], "02:00:00:00:00:20")
        self.assertIsNotNone(
            self.repository.blocked_device_for_candidate(
                {**candidate, "candidate_uuid": "moved", "ip": "192.0.2.99"}
            )
        )
        self.assertIsNone(
            self.repository.blocked_device_for_candidate(
                {
                    "candidate_uuid": "reused-address",
                    "ip": "192.0.2.20",
                    "mac": "02:00:00:00:00:99",
                    "onvif": {"endpoint_reference": "urn:uuid:different"},
                }
            )
        )
        self.assertTrue(self.repository.unblock_device(blocked["block_uuid"]))
        self.assertEqual(self.repository.blocked_devices(), [])

    def test_block_requires_nonconflicting_stable_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "no stable"):
            self.repository.block_candidate(
                {"candidate_uuid": "unstable", "display_name": "Unstable", "ip": "192.0.2.30"}
            )
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.repository.block_candidate(
                {
                    "candidate_uuid": "conflict",
                    "display_name": "Conflict",
                    "mac": "02:00:00:00:00:30",
                    "identity_conflict": True,
                }
            )

    def test_unadopt_removes_camera_children_and_saved_credentials(self) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-remove", "display_name": "Synthetic camera"},
            "operator",
            "synthetic-secret",
            [{
                "token": "main", "name": "Main", "uri": "rtsp://192.0.2.40/main",
                "width": 1280, "height": 720, "encoding": "H264", "fps": 15,
                "bitrate_kbps": 0,
            }],
            {"record": "main", "detect": "main"},
        )
        camera_uuid = adoption["camera_uuid"]

        self.assertTrue(self.repository.unadopt_camera(camera_uuid))
        self.assertIsNone(self.repository.camera(camera_uuid))
        self.assertIsNone(self.repository.adoption_for_candidate("candidate-remove"))
        with self.repository.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM camera_credentials").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM managed_streams").fetchone()[0], 0)

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

    def test_incident_schema_migration_preserves_existing_rows_and_foreign_keys(self) -> None:
        legacy_database = Path(self.temporary.name) / "legacy-incidents.db"
        with sqlite3.connect(legacy_database) as connection:
            connection.execute(
                "CREATE TABLE schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            for version, migration in enumerate(MIGRATIONS[:22], start=1):
                connection.executescript(migration)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, "2026-01-01T00:00:00+00:00"),
                )
            connection.execute(
                "INSERT INTO camera_credentials(credential_uuid, username, "
                "password_ciphertext, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    "credential-legacy",
                    "operator",
                    b"synthetic-ciphertext",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            connection.execute(
                "INSERT INTO cameras(camera_uuid, candidate_uuid, display_name, "
                "credential_uuid, adopted_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "camera-legacy",
                    "candidate-legacy",
                    "Synthetic legacy camera",
                    "credential-legacy",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            connection.execute(
                "INSERT INTO camera_incidents(incident_uuid, camera_uuid, kind, opened_at, "
                "last_observed_at) VALUES (?, ?, ?, ?, ?)",
                (
                    "incident-legacy",
                    "camera-legacy",
                    "media_offline",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            connection.execute(
                "INSERT INTO notification_outbox(outbox_uuid, incident_uuid, event_type, "
                "payload_json, idempotency_key, status, next_attempt_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "outbox-legacy",
                    "incident-legacy",
                    "incident_opened",
                    "{}",
                    "incident-legacy:incident_opened",
                    "pending",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            connection.commit()

        migrated = CameraRepository(legacy_database, b"k" * 32)
        migrated.migrate()
        with migrated.connect() as connection:
            incident = connection.execute(
                "SELECT kind, severity, details_json FROM camera_incidents "
                "WHERE incident_uuid = 'incident-legacy'"
            ).fetchone()
            outbox = connection.execute(
                "SELECT incident_uuid, event_type FROM notification_outbox "
                "WHERE outbox_uuid = 'outbox-legacy'"
            ).fetchone()
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(dict(incident), {
            "kind": "media_offline",
            "severity": "critical",
            "details_json": "{}",
        })
        self.assertEqual(dict(outbox), {
            "incident_uuid": "incident-legacy",
            "event_type": "incident_opened",
        })

    def test_address_recovery_closes_health_and_address_incidents_independently(self) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-moved", "display_name": "Synthetic camera"},
            "operator",
            "synthetic-secret",
            [{
                "token": "stream", "name": "Stream", "uri": "rtsp://192.0.2.70/live",
                "width": 1280, "height": 720, "encoding": "H264", "fps": 15,
                "bitrate_kbps": 0,
            }],
            {"record": "stream", "detect": "stream"},
        )
        camera_uuid = str(adoption["camera_uuid"])
        stream_uuid = str(adoption["streams"][0]["stream_uuid"])
        self.repository.record_probe_results({stream_uuid: ProbeResult("ready", 10)})
        for _ in range(3):
            self.repository.record_probe_results(
                {stream_uuid: ProbeResult("unavailable", 10)}
            )

        address_incident = self.repository.open_camera_address_incident(
            camera_uuid,
            "192.0.2.70",
            "192.0.2.71",
            "onvif-endpoint",
        )
        opened = self.repository.incidents(status="open")
        self.assertEqual(opened["open_count"], 2)
        self.assertEqual(
            {incident["kind"] for incident in opened["incidents"]},
            {"media_offline", "camera_address_changed"},
        )

        self.repository.record_probe_results({stream_uuid: ProbeResult("ready", 10)})
        health_recovered = self.repository.incidents(status="open")
        self.assertEqual(health_recovered["open_count"], 1)
        self.assertEqual(
            health_recovered["incidents"][0]["kind"],
            "camera_address_changed",
        )

        self.repository.resolve_camera_address_incident(address_incident)
        resolved = self.repository.incidents(status="resolved")
        self.assertEqual(resolved["open_count"], 0)
        reasons = {
            incident["kind"]: incident["resolution_reason"]
            for incident in resolved["incidents"]
        }
        self.assertEqual(reasons["media_offline"], "recovered")
        self.assertEqual(reasons["camera_address_changed"], "stream_recovered")

    def test_relay_restart_event_is_queued_once_without_camera_secrets(self) -> None:
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

        outbox_uuid = self.repository.enqueue_relay_restart_notification(
            reason="camera_address_recovery",
            camera_count=2,
        )

        self.assertIsNotNone(outbox_uuid)
        due = self.repository.due_notifications()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["event_type"], "relay_restarted")
        self.assertEqual(due[0]["payload"]["camera_count"], 2)
        self.assertEqual(due[0]["payload"]["reason"], "camera_address_recovery")
        self.assertNotIn("rtsp://", str(due))
        self.assertNotIn("synthetic-bot-token-value", str(due))

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

        self.repository.mark_frigate_binding_pending(
            "frigate-primary", adoption["camera_uuid"]
        )
        binding = self.repository.frigate_binding("frigate-primary", adoption["camera_uuid"])
        self.assertEqual(binding["status"], "pending")
        self.assertIsNone(binding["last_error_code"])

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
        self.assertEqual(self.repository.managed_stream_runtime_sources(), [])
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
        self.assertEqual(self.repository.managed_stream_runtime_sources(), [])

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
        self.assertEqual(len(self.repository.managed_stream_runtime_sources()), 1)

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

    def test_runtime_sources_are_role_bound_and_do_not_decrypt_credentials(self) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-runtime", "display_name": "Entrance"},
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "main",
                    "name": "Main",
                    "uri": "rtsp://192.0.2.20/main",
                    "width": 1920,
                    "height": 1080,
                    "encoding": "H264",
                    "fps": 20,
                    "bitrate_kbps": 2048,
                },
                {
                    "token": "extra",
                    "name": "Extra",
                    "uri": "rtsp://192.0.2.20/extra",
                    "width": 640,
                    "height": 360,
                    "encoding": "H264",
                    "fps": 10,
                    "bitrate_kbps": 512,
                },
            ],
            {"record": "main", "detect": "main"},
        )
        main = next(
            stream for stream in adoption["streams"] if stream["profile_token"] == "main"
        )

        with patch(
            "camadmiral.storage.decrypt_password",
            side_effect=AssertionError("runtime inventory must not decrypt credentials"),
        ):
            sources = self.repository.managed_stream_runtime_sources()

        self.assertEqual(
            sources,
            [
                {
                    "stream_uuid": main["stream_uuid"],
                    "stream_key": main["stream_key"],
                    "camera_uuid": adoption["camera_uuid"],
                }
            ],
        )
        self.assertNotIn("synthetic-secret", str(sources))
        self.assertNotIn("operator", str(sources))

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

    def test_identity_history_keeps_one_current_period_and_closes_changes(self) -> None:
        original = {
            "candidate_uuid": "candidate-identity",
            "display_name": "Synthetic camera",
            "ip": "192.0.2.20",
            "mac": "02:00:00:00:00:20",
            "onvif": {"endpoint_reference": "URN:UUID:SYNTHETIC-CAMERA"},
        }
        adoption = self.repository.adopt(
            original,
            "operator",
            "synthetic-secret",
            [{
                "token": "main", "name": "Main", "uri": "rtsp://192.0.2.20/main",
                "width": 1280, "height": 720, "encoding": "H264", "fps": 15,
                "bitrate_kbps": 0,
            }],
            {"record": "main", "detect": "main"},
        )
        camera_uuid = adoption["camera_uuid"]

        first = self.repository.camera_identity_history(camera_uuid)
        self.assertEqual(len(first), 1)
        self.assertEqual(
            {
                "ip": first[0]["ip"],
                "mac": first[0]["mac"],
                "onvif_identity": first[0]["onvif_identity"],
                "ended_at": first[0]["ended_at"],
                "current": first[0]["current"],
            },
            {
                "ip": "192.0.2.20",
                "mac": "02:00:00:00:00:20",
                "onvif_identity": "urn:uuid:synthetic-camera",
                "ended_at": None,
                "current": True,
            },
        )
        self.assertFalse(
            self.repository.observe_camera_identity(
                camera_uuid,
                {"candidate_uuid": "candidate-identity", "ip": "192.0.2.20"},
            )
        )

        changed_at = "2099-01-02T03:04:05+00:00"
        self.assertTrue(
            self.repository.observe_camera_identity(
                camera_uuid,
                {
                    **original,
                    "ip": "192.0.2.99",
                    "mac": "02:00:00:00:00:99",
                },
                observed_at=changed_at,
            )
        )
        history = self.repository.camera_identity_history(camera_uuid)

        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["ip"], "192.0.2.99")
        self.assertEqual(history[0]["mac"], "02:00:00:00:00:99")
        self.assertEqual(history[0]["started_at"], changed_at)
        self.assertTrue(history[0]["current"])
        self.assertEqual(history[1]["ip"], "192.0.2.20")
        self.assertEqual(history[1]["ended_at"], changed_at)
        self.assertFalse(history[1]["current"])

    def test_identity_history_initialization_does_not_advance_existing_period(self) -> None:
        original = {
            "candidate_uuid": "candidate-initialized",
            "display_name": "Synthetic camera",
            "ip": "192.0.2.20",
            "mac": "02:00:00:00:00:20",
            "onvif": {"endpoint_reference": "urn:uuid:synthetic-camera"},
        }
        adoption = self.repository.adopt(
            original,
            "operator",
            "synthetic-secret",
            [{
                "token": "main", "name": "Main", "uri": "rtsp://192.0.2.20/main",
                "width": 1280, "height": 720, "encoding": "H264", "fps": 15,
                "bitrate_kbps": 0,
            }],
            {"record": "main", "detect": "main"},
        )

        changes = self.repository.initialize_camera_identities(
            [(adoption["camera_uuid"], {**original, "ip": "192.0.2.99"})],
            observed_at="2099-01-02T03:04:05+00:00",
        )
        history = self.repository.camera_identity_history(adoption["camera_uuid"])

        self.assertEqual(changes, 0)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["ip"], "192.0.2.20")
        self.assertTrue(history[0]["current"])

    def test_identity_history_initializes_existing_camera_without_a_period(self) -> None:
        original = {
            "candidate_uuid": "candidate-migrated-identity",
            "display_name": "Synthetic camera",
            "ip": "192.0.2.20",
            "mac": "02:00:00:00:00:20",
            "onvif": {"endpoint_reference": "urn:uuid:synthetic-camera"},
        }
        adoption = self.repository.adopt(
            original,
            "operator",
            "synthetic-secret",
            [{
                "token": "main", "name": "Main", "uri": "rtsp://192.0.2.20/main",
                "width": 1280, "height": 720, "encoding": "H264", "fps": 15,
                "bitrate_kbps": 0,
            }],
            {"record": "main", "detect": "main"},
        )
        with self.repository.connect() as connection:
            connection.execute(
                "DELETE FROM camera_identity_periods WHERE camera_uuid = ?",
                (adoption["camera_uuid"],),
            )
            connection.commit()

        changes = self.repository.initialize_camera_identities(
            [(adoption["camera_uuid"], original)],
            observed_at="2099-01-02T03:04:05+00:00",
        )
        history = self.repository.camera_identity_history(adoption["camera_uuid"])

        self.assertEqual(changes, 1)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["ip"], "192.0.2.20")
        self.assertTrue(history[0]["current"])

    def test_identity_history_keeps_unobserved_fields_unknown_after_change(self) -> None:
        original = {
            "candidate_uuid": "candidate-partial-identity",
            "display_name": "Synthetic camera",
            "ip": "192.0.2.20",
            "mac": "02:00:00:00:00:20",
            "onvif": {"endpoint_reference": "urn:uuid:synthetic-camera"},
        }
        adoption = self.repository.adopt(
            original,
            "operator",
            "synthetic-secret",
            [{
                "token": "main", "name": "Main", "uri": "rtsp://192.0.2.20/main",
                "width": 1280, "height": 720, "encoding": "H264", "fps": 15,
                "bitrate_kbps": 0,
            }],
            {"record": "main", "detect": "main"},
        )

        changed = self.repository.observe_camera_identity(
            adoption["camera_uuid"],
            {
                "candidate_uuid": "candidate-partial-identity",
                "ip": "192.0.2.99",
                "onvif": {"endpoint_reference": "urn:uuid:synthetic-camera"},
            },
        )
        history = self.repository.camera_identity_history(adoption["camera_uuid"])

        self.assertTrue(changed)
        self.assertEqual(history[0]["ip"], "192.0.2.99")
        self.assertIsNone(history[0]["mac"])
        self.assertEqual(history[0]["onvif_identity"], "urn:uuid:synthetic-camera")

    def test_identity_history_enriches_unknown_current_fields_without_new_period(self) -> None:
        candidate = {
            "candidate_uuid": "candidate-enriched-identity",
            "display_name": "Synthetic camera",
            "ip": "192.0.2.20",
        }
        adoption = self.repository.adopt(
            candidate,
            "operator",
            "synthetic-secret",
            [{
                "token": "main", "name": "Main", "uri": "rtsp://192.0.2.20/main",
                "width": 1280, "height": 720, "encoding": "H264", "fps": 15,
                "bitrate_kbps": 0,
            }],
            {"record": "main", "detect": "main"},
        )

        enriched = self.repository.observe_camera_identity(
            adoption["camera_uuid"],
            {
                **candidate,
                "mac": "02:00:00:00:00:20",
                "onvif": {"endpoint_reference": "urn:uuid:synthetic-camera"},
            },
        )
        history = self.repository.camera_identity_history(adoption["camera_uuid"])

        self.assertTrue(enriched)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["mac"], "02:00:00:00:00:20")
        self.assertEqual(history[0]["onvif_identity"], "urn:uuid:synthetic-camera")

    def test_inventory_observation_records_same_ip_mac_change(self) -> None:
        original = {
            "candidate_uuid": "candidate-same-ip-new-mac",
            "display_name": "Synthetic camera",
            "ip": "172.21.10.20",
            "mac": "02:00:00:00:00:20",
            "onvif": {"endpoint_reference": "urn:uuid:synthetic-camera"},
        }
        adoption = self.repository.adopt(
            original,
            "operator",
            "synthetic-secret",
            [{
                "token": "main", "name": "Main", "uri": "rtsp://172.21.10.20/main",
                "width": 1280, "height": 720, "encoding": "H264", "fps": 15,
                "bitrate_kbps": 0,
            }],
            {"record": "main", "detect": "main"},
        )
        camera_uuid = adoption["camera_uuid"]
        changed_at = "2099-01-02T03:04:05+00:00"

        changes = self.repository.observe_inventory_identities(
            [(camera_uuid, {**original, "mac": "02:00:00:00:00:21"})],
            advance_existing_camera_uuids={camera_uuid},
            observed_at=changed_at,
        )
        history = self.repository.camera_identity_history(camera_uuid)

        self.assertEqual(changes, 1)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["ip"], "172.21.10.20")
        self.assertEqual(history[0]["mac"], "02:00:00:00:00:21")
        self.assertEqual(history[0]["onvif_identity"], "urn:uuid:synthetic-camera")
        self.assertEqual(history[0]["started_at"], changed_at)
        self.assertEqual(history[1]["mac"], "02:00:00:00:00:20")
        self.assertEqual(history[1]["ended_at"], changed_at)

    def test_inventory_observation_records_same_ip_onvif_change(self) -> None:
        original = {
            "candidate_uuid": "candidate-same-ip-new-onvif",
            "display_name": "Synthetic camera",
            "ip": "172.21.10.30",
            "mac": "02:00:00:00:00:30",
            "onvif": {"endpoint_reference": "urn:uuid:synthetic-camera-old"},
        }
        adoption = self.repository.adopt(
            original,
            "operator",
            "synthetic-secret",
            [{
                "token": "main", "name": "Main", "uri": "rtsp://172.21.10.30/main",
                "width": 1280, "height": 720, "encoding": "H264", "fps": 15,
                "bitrate_kbps": 0,
            }],
            {"record": "main", "detect": "main"},
        )
        camera_uuid = adoption["camera_uuid"]

        changes = self.repository.observe_inventory_identities(
            [(
                camera_uuid,
                {
                    **original,
                    "onvif": {"endpoint_reference": "urn:uuid:synthetic-camera-new"},
                },
            )],
            advance_existing_camera_uuids={camera_uuid},
        )
        history = self.repository.camera_identity_history(camera_uuid)

        self.assertEqual(changes, 1)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["ip"], "172.21.10.30")
        self.assertEqual(history[0]["mac"], "02:00:00:00:00:30")
        self.assertEqual(history[0]["onvif_identity"], "urn:uuid:synthetic-camera-new")
        self.assertEqual(history[1]["onvif_identity"], "urn:uuid:synthetic-camera-old")

    def test_identity_history_returns_none_for_unknown_camera(self) -> None:
        self.assertIsNone(self.repository.camera_identity_history("missing-camera"))

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
