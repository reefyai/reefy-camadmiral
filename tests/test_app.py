import json
import ipaddress
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

from starlette.requests import Request

from camadmiral import app as app_module
from camadmiral.frigate import FrigateTarget
from camadmiral.discovery import LanInterface
from camadmiral.onvif_client import OnvifInspectionError
from camadmiral.media import ProbeResult
from camadmiral.rtsp_catalog import CatalogCandidate
from camadmiral.storage import MIGRATIONS, CameraRepository


def synthetic_candidate(*, amcrest: bool = True) -> dict:
    return {
        "candidate_uuid": "candidate-1",
        "display_name": "Amcrest camera" if amcrest else "Synthetic camera",
        "status": "online",
        "onvif": {
            "name": "Amcrest" if amcrest else "Synthetic",
            "model": "Model 42",
            "service_urls": ["http://192.168.10.20/onvif/device_service"],
        },
    }


def inspection() -> dict:
    return {
        "status": "ok",
        "profiles": [
            {
                "token": "profile-1",
                "name": "Profile 1",
                "uri": "rtsp://192.168.10.20/stream",
                "width": 1920,
                "height": 1080,
                "encoding": "H264",
                "fps": 20,
                "bitrate_kbps": 2048,
            }
        ],
    }


class FakeRepository:
    def __init__(self) -> None:
        self.saved_credentials = None

    def adopt(self, _candidate, username, password, _profiles, _roles):
        self.saved_credentials = (username, password)
        return {"camera_id": "camera-1", "streams": []}

    def adoption_for_candidate(self, _candidate_uuid):
        if self.saved_credentials is None:
            return None
        return {"camera_id": "camera-1", "streams": []}

    def frigate_targets(self, *, sync_only=False):
        return []


class DiscoveryDecorationTests(unittest.TestCase):
    def test_media_access_reports_current_lan_host_for_url_previews(self) -> None:
        repository = Mock()
        repository.rtsp_access_password.return_value = "synthetic-secret"

        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(
                app_module,
                "media_host_for_mode",
                return_value="192.168.50.12",
            ) as resolve_host,
        ):
            response = app_module.media_access("reveal-media-access")

        payload = json.loads(response.body)
        self.assertEqual(payload["lan_host"], "192.168.50.12")
        resolve_host.assert_called_once_with(app_module.INVENTORY, "lan")

    def test_media_access_remains_available_when_lan_host_is_unknown(self) -> None:
        repository = Mock()
        repository.rtsp_access_password.return_value = "synthetic-secret"

        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(
                app_module,
                "media_host_for_mode",
                side_effect=app_module.FrigateApiError("media_host_unavailable"),
            ),
        ):
            response = app_module.media_access("reveal-media-access")

        payload = json.loads(response.body)
        self.assertEqual(payload["status"], "ok")
        self.assertIsNone(payload["lan_host"])

    def test_app_icon_is_served_for_header_and_browser_tab(self) -> None:
        response = app_module.app_icon()
        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(Path(response.path), app_module.ICON)
        self.assertTrue(app_module.ICON.is_file())

    def test_index_csp_allows_in_memory_player_media_only(self) -> None:
        response = app_module.index()
        policy = response.headers["content-security-policy"]
        self.assertIn("media-src 'self' blob:", policy)
        self.assertIn("img-src 'self' data: blob:", policy)
        self.assertEqual(policy.count("blob:"), 2)

    def test_navigation_page_routes_are_registered(self) -> None:
        routes = {
            (route.path, method)
            for route in app_module.app.routes
            for method in getattr(route, "methods", set())
        }
        self.assertIn(("/settings", "GET"), routes)
        self.assertIn(("/settings/notifications", "GET"), routes)
        self.assertIn(("/settings/integrations", "GET"), routes)
        self.assertIn(("/incidents", "GET"), routes)

    def test_adopted_name_replaces_scanner_name(self) -> None:
        repository = Mock()
        repository.blocked_devices.return_value = []
        repository.adoption_map.return_value = {
            "candidate-1": {"display_name": "Operator name", "streams": []}
        }
        repository.frigate_targets.return_value = []
        state = {
            "devices": [
                {
                    "candidate_uuid": "candidate-1",
                    "display_name": "192.0.2.10",
                }
            ]
        }

        with patch.object(app_module, "_repository", return_value=repository):
            decorated = app_module._decorate_adoptions(state)

        self.assertEqual(decorated["devices"][0]["display_name"], "Operator name")

    def test_recovered_media_overrides_stale_offline_scan_in_summary(self) -> None:
        repository = Mock()
        repository.blocked_devices.return_value = []
        repository.adoption_map.return_value = {
            "candidate-1": {
                "camera_uuid": "camera-1",
                "display_name": "Recovered camera",
                "enabled": True,
                "roles": {"record": "stream-1", "detect": "stream-1"},
                "streams": [
                    {
                        "stream_uuid": "stream-1",
                        "health_status": "healthy",
                    }
                ],
            }
        }
        state = {
            "devices": [
                {
                    "candidate_uuid": "candidate-1",
                    "display_name": "Recovered camera",
                    "status": "offline",
                }
            ],
            "summary": {"devices": 1, "online": 0, "offline": 1},
        }

        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "load_frigate_targets", return_value=[]),
            patch.object(
                app_module.RELAY_HEALTH_MONITOR,
                "cached_frame",
                return_value=None,
            ),
        ):
            decorated = app_module._decorate_adoptions(state)

        device = decorated["devices"][0]
        self.assertEqual(device["status"], "offline")
        self.assertEqual(device["connectivity_status"], "online")
        self.assertEqual(
            decorated["summary"],
            {"devices": 1, "online": 1, "offline": 0, "blocked": 0},
        )

    def test_matching_offline_identity_can_initialize_from_inventory(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {
            "candidate-1": {
                "camera_uuid": "camera-1",
                "streams": [
                    {"uri": "rtsp://172.21.10.20/main"},
                    {"uri": "rtsp://172.21.10.20/sub"},
                ],
            }
        }
        candidate = {
            "candidate_uuid": "candidate-1",
            "display_name": "Synthetic camera",
            "ip": "172.21.10.20",
            "mac": "02:00:00:00:00:20",
            "onvif": {"endpoint_reference": "urn:uuid:synthetic-camera"},
            "status": "offline",
        }

        with patch.object(
            app_module,
            "_inventory_candidates",
            return_value={"candidate-1": candidate},
        ):
            app_module._observe_inventory_identities(repository)

        observations = repository.observe_inventory_identities.call_args.args[0]
        self.assertEqual(observations[0][0], "camera-1")
        self.assertEqual(observations[0][1]["ip"], "172.21.10.20")
        self.assertEqual(
            repository.observe_inventory_identities.call_args.kwargs[
                "advance_existing_camera_uuids"
            ],
            set(),
        )

    def test_previous_schema_initializes_identity_history_through_health_observation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = CameraRepository(Path(directory) / "camadmiral.db", b"k" * 32)
            repository.migrate()
            candidate = {
                "candidate_uuid": "candidate-upgrade",
                "display_name": "Synthetic camera",
                "ip": "172.21.10.20",
                "mac": "02:00:00:00:00:20",
                "status": "offline",
                "onvif": {"endpoint_reference": "urn:uuid:synthetic-camera"},
            }
            adoption = repository.adopt(
                candidate,
                "operator",
                "synthetic-secret",
                [{
                    "token": "main",
                    "name": "Main",
                    "uri": "rtsp://172.21.10.20/main",
                    "width": 1280,
                    "height": 720,
                    "encoding": "H264",
                    "fps": 15,
                    "bitrate_kbps": 0,
                }],
                {"record": "main", "detect": "main"},
            )
            with repository.connect() as connection:
                connection.execute("DROP TABLE camera_identity_periods")
                connection.execute(
                    "DELETE FROM schema_migrations WHERE version = ?",
                    (len(MIGRATIONS),),
                )
                connection.commit()

            repository.migrate()
            self.assertEqual(
                repository.camera_identity_history(adoption["camera_uuid"]),
                [],
            )
            with patch.object(
                app_module,
                "_inventory_candidates",
                return_value={"candidate-upgrade": candidate},
            ):
                app_module._observe_inventory_identities(repository)

            history = repository.camera_identity_history(adoption["camera_uuid"])
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["ip"], "172.21.10.20")
            self.assertEqual(history[0]["mac"], "02:00:00:00:00:20")
            self.assertEqual(
                history[0]["onvif_identity"],
                "urn:uuid:synthetic-camera",
            )
            self.assertTrue(history[0]["current"])

    def test_matching_online_identity_can_advance_from_inventory(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {
            "candidate-1": {
                "camera_uuid": "camera-1",
                "streams": [{"uri": "rtsp://172.21.10.20/main"}],
            }
        }
        candidate = {
            "candidate_uuid": "candidate-1",
            "ip": "172.21.10.20",
            "mac": "02:00:00:00:00:21",
            "status": "online",
        }

        with patch.object(
            app_module,
            "_inventory_candidates",
            return_value={"candidate-1": candidate},
        ):
            app_module._observe_inventory_identities(repository)

        self.assertEqual(
            repository.observe_inventory_identities.call_args.kwargs[
                "advance_existing_camera_uuids"
            ],
            {"camera-1"},
        )

    def test_mismatched_recovery_address_cannot_initialize_identity_history(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {
            "candidate-1": {
                "camera_uuid": "camera-1",
                "streams": [{"uri": "rtsp://172.21.10.20/main"}],
            }
        }
        failed_recovery_observation = {
            "candidate_uuid": "candidate-1",
            "ip": "172.21.10.99",
            "mac": "02:00:00:00:00:99",
            "status": "online",
        }

        with patch.object(
            app_module,
            "_inventory_candidates",
            return_value={"candidate-1": failed_recovery_observation},
        ):
            app_module._observe_inventory_identities(repository)

        repository.observe_inventory_identities.assert_called_once_with(
            [],
            advance_existing_camera_uuids=set(),
        )

    def test_conflicted_identity_is_not_observed(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {
            "candidate-1": {
                "camera_uuid": "camera-1",
                "streams": [{"uri": "rtsp://172.21.10.20/main"}],
            }
        }
        candidate = {
            "candidate_uuid": "candidate-1",
            "ip": "172.21.10.20",
            "mac": "02:00:00:00:00:20",
            "status": "online",
            "identity_conflict": True,
        }

        with patch.object(
            app_module,
            "_inventory_candidates",
            return_value={"candidate-1": candidate},
        ):
            app_module._observe_inventory_identities(repository)

        repository.observe_inventory_identities.assert_called_once_with(
            [],
            advance_existing_camera_uuids=set(),
        )

    def test_discovery_reports_only_an_available_cached_thumbnail(self) -> None:
        repository = Mock()
        repository.blocked_devices.return_value = []
        repository.adoption_map.return_value = {
            "candidate-1": {
                "camera_uuid": "camera-1",
                "display_name": "Operator name",
                "streams": [],
            }
        }
        state = {"devices": [{"candidate_uuid": "candidate-1"}]}
        frame = Mock(captured_at=1234.5)

        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module.RELAY_HEALTH_MONITOR, "cached_frame", return_value=frame),
            patch.object(app_module, "load_frigate_targets", return_value=[]),
        ):
            decorated = app_module._decorate_adoptions(state)

        self.assertEqual(
            decorated["devices"][0]["adoption"]["thumbnail_captured_at"],
            1234.5,
        )

    def test_each_synced_frigate_target_has_an_independent_status(self) -> None:
        repository = Mock()
        repository.blocked_devices.return_value = []
        repository.adoption_map.return_value = {
            "candidate-1": {
                "camera_uuid": "camera-1",
                "display_name": "Operator name",
                "streams": [],
            }
        }
        repository.frigate_bindings.side_effect = [
            [{"camera_uuid": "camera-1", "status": "applied"}],
            [],
        ]
        repository.frigate_camera_selections.side_effect = [
            [{"camera_uuid": "camera-1", "address_mode": "localhost"}],
            [],
        ]
        state = {
            "devices": [
                {
                    "candidate_uuid": "candidate-1",
                    "display_name": "Synthetic camera",
                }
            ]
        }
        targets = [
            FrigateTarget("one", "Frigate One", "http://127.0.0.1:20001"),
            FrigateTarget("two", "Frigate Two", "http://127.0.0.1:20002"),
        ]

        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "load_frigate_targets", return_value=targets),
        ):
            decorated = app_module._decorate_adoptions(state)

        self.assertEqual(
            decorated["devices"][0]["adoption"]["frigate"],
            [
                {
                    "target_id": "one",
                    "target": "Frigate One",
                    "selected": True,
                    "address_mode": "localhost",
                    "status": "applied",
                },
                {
                    "target_id": "two",
                    "target": "Frigate Two",
                    "selected": False,
                    "address_mode": "lan",
                    "status": "not_synced",
                },
            ],
        )

    def test_frigate_status_includes_safe_internal_error_code(self) -> None:
        repository = Mock()
        repository.blocked_devices.return_value = []
        repository.adoption_map.return_value = {
            "candidate-1": {
                "camera_uuid": "camera-1",
                "display_name": "Operator name",
                "streams": [],
            }
        }
        repository.frigate_bindings.return_value = [
            {
                "camera_uuid": "camera-1",
                "status": "error",
                "last_error_code": "camera_start_pending",
            }
        ]
        repository.frigate_camera_selections.return_value = [
            {"camera_uuid": "camera-1", "address_mode": "lan"}
        ]
        state = {
            "devices": [
                {
                    "candidate_uuid": "candidate-1",
                    "display_name": "Synthetic camera",
                }
            ]
        }
        targets = [FrigateTarget("one", "Frigate One", "http://127.0.0.1:20001")]

        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "load_frigate_targets", return_value=targets),
        ):
            decorated = app_module._decorate_adoptions(state)

        self.assertEqual(
            decorated["devices"][0]["adoption"]["frigate"],
            [
                {
                    "target_id": "one",
                    "target": "Frigate One",
                    "selected": True,
                    "address_mode": "lan",
                    "status": "error",
                    "error_code": "camera_start_pending",
                }
            ],
        )

    def test_blocked_devices_are_excluded_from_regular_summary(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {}
        repository.blocked_devices.return_value = [
            {
                "block_uuid": "block-1",
                "candidate_uuid": "candidate-1",
                "onvif_identity": "urn:uuid:blocked-1",
                "mac": "02:00:00:00:00:41",
                "display_name": "Synthetic blocked device",
                "last_ip": "192.0.2.41",
            }
        ]
        state = {
            "devices": [
                {
                    "candidate_uuid": "candidate-1",
                    "display_name": "Synthetic blocked device",
                    "ip": "192.0.2.99",
                    "mac": "02:00:00:00:00:41",
                    "onvif": {"endpoint_reference": "urn:uuid:blocked-1"},
                    "status": "online",
                },
                {
                    "candidate_uuid": "candidate-2",
                    "display_name": "Visible device",
                    "status": "online",
                },
            ]
        }

        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "load_frigate_targets", return_value=[]),
        ):
            decorated = app_module._decorate_adoptions(state)

        self.assertTrue(decorated["devices"][0]["blocked"])
        self.assertEqual(
            decorated["summary"],
            {"devices": 1, "online": 1, "offline": 0, "blocked": 1},
        )


class AvailabilityApiTests(unittest.TestCase):
    def test_supported_window_uses_bounded_bucket_count(self) -> None:
        repository = Mock()
        repository.camera_availability.return_value = {
            "window": "168h",
            "start": "2026-01-01T00:00:00+00:00",
            "end": "2026-01-08T00:00:00+00:00",
            "availability_percent": 99.5,
            "observed_seconds": 604800,
            "buckets": [],
        }
        with patch.object(app_module, "_repository", return_value=repository):
            response = app_module.camera_availability("camera-1", "7d")

        payload = json.loads(response.body)
        repository.camera_availability.assert_called_once_with(
            "camera-1",
            hours=168,
            bucket_count=56,
        )
        self.assertEqual(payload["window"], "7d")
        self.assertEqual(payload["availability_percent"], 99.5)

    def test_unsupported_window_is_rejected_before_storage_access(self) -> None:
        with patch.object(app_module, "_repository") as repository:
            response = app_module.camera_availability("camera-1", "30d")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(json.loads(response.body)["status"], "invalid_window")
        repository.assert_not_called()


class IdentityHistoryApiTests(unittest.TestCase):
    def test_identity_history_returns_camera_periods(self) -> None:
        repository = Mock()
        repository.camera_identity_history.return_value = [
            {
                "ip": "192.0.2.20",
                "mac": "02:00:00:00:00:20",
                "onvif_identity": "urn:uuid:synthetic-camera",
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": None,
                "current": True,
            }
        ]
        with patch.object(app_module, "_repository", return_value=repository):
            response = app_module.camera_identity_history("camera-1")

        payload = json.loads(response.body)
        self.assertEqual(payload["camera_uuid"], "camera-1")
        self.assertTrue(payload["periods"][0]["current"])
        repository.camera_identity_history.assert_called_once_with("camera-1")

    def test_identity_history_rejects_unknown_camera(self) -> None:
        repository = Mock()
        repository.camera_identity_history.return_value = None
        with (
            patch.object(app_module, "_repository", return_value=repository),
            self.assertRaises(app_module.HTTPException) as raised,
        ):
            app_module.camera_identity_history("missing-camera")

        self.assertEqual(raised.exception.status_code, 404)


class CameraLifecycleApiTests(unittest.TestCase):
    @staticmethod
    def payload(response) -> dict:
        return json.loads(response.body)

    def test_block_uses_repository_stable_identity_policy(self) -> None:
        repository = Mock()
        repository.adoption_for_candidate.return_value = None
        repository.block_candidate.return_value = {"block_uuid": "block-1"}
        candidate = synthetic_candidate(amcrest=False) | {"mac": "02:00:00:00:00:50"}
        with (
            patch.object(app_module, "_find_candidate", return_value=candidate),
            patch.object(app_module, "_repository", return_value=repository),
        ):
            response = app_module.block_candidate("candidate-1", "block-camera")

        self.assertEqual(response.status_code, 200)
        repository.block_candidate.assert_called_once_with(candidate)

    def test_block_rejects_adopted_camera(self) -> None:
        repository = Mock()
        repository.adoption_for_candidate.return_value = {"camera_uuid": "camera-1"}
        with (
            patch.object(app_module, "_find_candidate", return_value=synthetic_candidate()),
            patch.object(app_module, "_repository", return_value=repository),
        ):
            response = app_module.block_candidate("candidate-1", "block-camera")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.payload(response)["status"], "adopted")
        repository.block_candidate.assert_not_called()

    def test_unadopt_cleans_frigate_then_local_camera_and_media(self) -> None:
        repository = Mock()
        repository.camera.return_value = {"camera_uuid": "camera-1"}
        repository.selected_frigate_camera_uuids.return_value = ["camera-1"]
        repository.unadopt_camera.return_value = True
        target = FrigateTarget("frigate-1", "Synthetic Frigate", "http://127.0.0.1:20001")
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "load_frigate_targets", return_value=[target]),
            patch.object(
                app_module,
                "remove_frigate_camera",
                return_value={"restart_recommended": True},
            ) as remove,
            patch.object(app_module, "_reconcile_media") as reconcile,
        ):
            response = app_module.unadopt_camera("camera-1", "unadopt-camera")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.payload(response)["restart_recommended"])
        remove.assert_called_once_with(repository, target, "camera-1")
        repository.unadopt_camera.assert_called_once_with("camera-1")
        reconcile.assert_called_once_with()

    def test_unadopt_keeps_local_camera_when_frigate_cleanup_fails(self) -> None:
        repository = Mock()
        repository.camera.return_value = {"camera_uuid": "camera-1"}
        repository.selected_frigate_camera_uuids.return_value = ["camera-1"]
        target = FrigateTarget("frigate-1", "Synthetic Frigate", "http://127.0.0.1:20001")
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "load_frigate_targets", return_value=[target]),
            patch.object(
                app_module,
                "remove_frigate_camera",
                side_effect=app_module.FrigateApiError("synthetic_failure"),
            ),
        ):
            response = app_module.unadopt_camera("camera-1", "unadopt-camera")

        self.assertGreaterEqual(response.status_code, 400)
        repository.unadopt_camera.assert_not_called()

    def test_unadopt_keeps_recently_reachable_candidate_online(self) -> None:
        repository = Mock()
        repository.camera.return_value = {
            "camera_uuid": "camera-1",
            "candidate_uuid": "candidate-1",
        }
        repository.unadopt_camera.return_value = True
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "load_frigate_targets", return_value=[]),
            patch.object(app_module, "_candidate_is_currently_online", return_value=True),
            patch.object(app_module, "_mark_discovery_candidate_online") as mark_online,
            patch.object(app_module, "_reconcile_media"),
        ):
            response = app_module.unadopt_camera("camera-1", "unadopt-camera")

        self.assertEqual(response.status_code, 200)
        mark_online.assert_called_once_with("candidate-1")

    def test_recently_reachable_candidate_updates_inventory_and_scan_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            inventory = Path(temporary_directory) / "inventory.json"
            scan_state = Path(temporary_directory) / "scan-state.json"
            payload = {
                "devices": [
                    {
                        "candidate_uuid": "candidate-1",
                        "status": "offline",
                        "missed_scans": 2,
                    },
                    {"candidate_uuid": "candidate-2", "status": "offline"},
                ]
            }
            inventory.write_text(json.dumps(payload), encoding="utf-8")
            scan_state.write_text(json.dumps(payload), encoding="utf-8")
            with (
                patch.object(app_module, "INVENTORY", inventory),
                patch.object(app_module, "SCAN_STATE", scan_state),
            ):
                app_module._mark_discovery_candidate_online("candidate-1")

            for path in (inventory, scan_state):
                updated = json.loads(path.read_text(encoding="utf-8"))
                candidate = updated["devices"][0]
                self.assertEqual(candidate["status"], "online")
                self.assertEqual(candidate["missed_scans"], 0)
                self.assertEqual(updated["summary"]["online"], 1)
                self.assertEqual(updated["summary"]["offline"], 1)


class IncidentAndNotificationApiTests(unittest.TestCase):
    def test_incident_filter_and_limit_are_bounded(self) -> None:
        repository = Mock()
        repository.incidents.return_value = {"open_count": 1, "incidents": []}
        with patch.object(app_module, "_repository", return_value=repository):
            response = app_module.incidents("all", 100)
        self.assertEqual(response.status_code, 200)
        repository.incidents.assert_called_once_with(status="all", limit=100)

        with patch.object(app_module, "_repository") as untouched:
            invalid = app_module.incidents("unexpected", 50)
        self.assertEqual(invalid.status_code, 422)
        untouched.assert_not_called()

    def test_notification_update_requires_action_header(self) -> None:
        request = app_module.NotificationSettingsRequest()
        with self.assertRaises(app_module.HTTPException) as raised:
            app_module.update_notification_settings(request, None)
        self.assertEqual(raised.exception.status_code, 400)

    def test_new_bot_is_validated_and_secret_is_not_returned(self) -> None:
        repository = Mock()
        repository.notification_credentials.return_value = None
        repository.notification_settings.return_value = {
            "provider": "telegram",
            "enabled": True,
            "bot_configured": True,
            "bot_username": "synthetic_alert_bot",
            "connection_status": "waiting_for_start",
            "destination": None,
            "last_delivery": None,
        }
        client = Mock()
        client.identity.return_value = {"id": 123, "username": "synthetic_alert_bot"}
        client.webhook.return_value = {"url": ""}
        request = app_module.NotificationSettingsRequest(
            enabled=True,
            telegram_bot_token="123456:synthetic-bot-token-value",
        )
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "TelegramClient", return_value=client),
            patch.object(app_module.secrets, "token_urlsafe", return_value="synthetic-pairing"),
        ):
            response = app_module.update_notification_settings(
                request,
                "update-notification-settings",
            )

        payload = json.loads(response.body)
        self.assertEqual(payload["provider"], "telegram")
        self.assertNotIn("telegram_bot_token", payload)
        self.assertNotIn("synthetic-bot-token-value", response.body.decode())
        repository.save_telegram_settings.assert_called_once()
        self.assertTrue(repository.save_telegram_settings.call_args.kwargs["enabled"])

    def test_existing_clients_cannot_disable_configured_telegram_alerts(self) -> None:
        repository = Mock()
        repository.notification_credentials.return_value = {
            "bot_token": "123456:synthetic-bot-token-value",
            "chat_id": "100200300",
        }
        repository.notification_settings.return_value = {
            "provider": "telegram",
            "enabled": True,
            "bot_configured": True,
            "bot_username": "synthetic_alert_bot",
            "connection_status": "connected",
            "destination": "Synthetic operator",
            "last_delivery": None,
        }
        with patch.object(app_module, "_repository", return_value=repository):
            response = app_module.update_notification_settings(
                app_module.NotificationSettingsRequest(enabled=False),
                "update-notification-settings",
            )

        self.assertEqual(response.status_code, 200)
        repository.save_telegram_settings.assert_called_once_with(
            enabled=True,
            bot_token=None,
            bot_id=None,
            bot_username=None,
            pairing_token=None,
            pairing_expires_at=None,
        )

    def test_bot_with_existing_webhook_is_rejected_without_modifying_it(self) -> None:
        repository = Mock()
        repository.notification_credentials.return_value = None
        client = Mock()
        client.identity.return_value = {"id": 123, "username": "shared_bot"}
        client.webhook.return_value = {"url": "https://example.invalid/receiver"}
        request = app_module.NotificationSettingsRequest(
            enabled=True,
            telegram_bot_token="123456:synthetic-bot-token-value",
        )
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "TelegramClient", return_value=client),
        ):
            response = app_module.update_notification_settings(
                request,
                "update-notification-settings",
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)["status"], "bot_has_webhook")
        repository.save_telegram_settings.assert_not_called()


class DiscoveryNetworkApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.detected = LanInterface(
            "eth0",
            ipaddress.IPv4Address("192.168.40.2"),
            ipaddress.IPv4Network("192.168.40.0/24"),
        )

    def test_detected_private_subnets_are_selected_by_default(self) -> None:
        repository = Mock()
        repository.discovery_network_settings.return_value = {
            "custom_subnets": [],
            "excluded_detected_subnets": [],
        }
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "private_lan_interfaces", return_value=[self.detected]),
        ):
            response = app_module.discovery_networks()

        payload = json.loads(response.body)
        self.assertEqual(payload["max_custom_hosts"], 1024)
        self.assertEqual(payload["networks"][0]["cidr"], "192.168.40.0/24")
        self.assertTrue(payload["networks"][0]["selected"])
        self.assertTrue(payload["networks"][0]["multicast"])

    def test_save_normalizes_custom_subnet_and_excludes_removed_detected_subnet(self) -> None:
        repository = Mock()
        repository.discovery_network_settings.return_value = {
            "custom_subnets": [],
            "excluded_detected_subnets": [],
        }
        result_payload = {"networks": [], "max_custom_hosts": 1024}
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "private_lan_interfaces", return_value=[self.detected]),
            patch.object(
                app_module,
                "_discovery_network_configuration",
                return_value=result_payload,
            ),
        ):
            response = app_module.update_discovery_networks(
                app_module.DiscoveryNetworksRequest(
                    selected_subnets=["10.0.202.15/24"]
                ),
                "save-discovery-networks",
            )

        self.assertEqual(response.status_code, 200)
        repository.save_discovery_network_settings.assert_called_once_with(
            custom_subnets=["10.0.202.0/24"],
            excluded_detected_subnets=["192.168.40.0/24"],
            excluded_custom_subnets=[],
        )

    def test_save_keeps_an_unselected_custom_subnet(self) -> None:
        repository = Mock()
        repository.discovery_network_settings.return_value = {
            "custom_subnets": [],
            "excluded_detected_subnets": [],
            "excluded_custom_subnets": [],
        }
        result_payload = {"networks": [], "max_custom_hosts": 1024}
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "private_lan_interfaces", return_value=[self.detected]),
            patch.object(
                app_module,
                "_discovery_network_configuration",
                return_value=result_payload,
            ),
        ):
            response = app_module.update_discovery_networks(
                app_module.DiscoveryNetworksRequest(
                    selected_subnets=["192.168.40.0/24"],
                    custom_subnets=["10.0.202.15/24"],
                ),
                "save-discovery-networks",
            )

        self.assertEqual(response.status_code, 200)
        repository.save_discovery_network_settings.assert_called_once_with(
            custom_subnets=["10.0.202.0/24"],
            excluded_detected_subnets=[],
            excluded_custom_subnets=["10.0.202.0/24"],
        )

    def test_custom_subnet_checkbox_state_is_returned(self) -> None:
        repository = Mock()
        routed = LanInterface(
            "eth0",
            ipaddress.IPv4Address("192.168.40.2"),
            ipaddress.IPv4Network("10.0.202.0/24"),
            directly_connected=False,
        )
        repository.discovery_network_settings.return_value = {
            "custom_subnets": ["10.0.202.0/24"],
            "excluded_detected_subnets": [],
            "excluded_custom_subnets": ["10.0.202.0/24"],
        }
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "private_lan_interfaces", return_value=[self.detected]),
            patch.object(app_module, "routed_scan_interface", return_value=routed),
        ):
            response = app_module.discovery_networks()

        payload = json.loads(response.body)
        custom = next(network for network in payload["networks"] if network["source"] == "custom")
        self.assertFalse(custom["selected"])

    def test_save_rejects_public_or_oversized_custom_subnets(self) -> None:
        for subnet in ("192.0.2.0/24", "10.0.0.0/8"):
            repository = Mock()
            repository.discovery_network_settings.return_value = {
                "custom_subnets": [],
                "excluded_detected_subnets": [],
            }
            with (
                self.subTest(subnet=subnet),
                patch.object(app_module, "_repository", return_value=repository),
                patch.object(app_module, "private_lan_interfaces", return_value=[self.detected]),
            ):
                response = app_module.update_discovery_networks(
                    app_module.DiscoveryNetworksRequest(selected_subnets=[subnet]),
                    "save-discovery-networks",
                )

            self.assertEqual(response.status_code, 422)
            repository.save_discovery_network_settings.assert_not_called()

    def test_scan_request_captures_saved_subnets(self) -> None:
        repository = Mock()
        with tempfile.TemporaryDirectory() as directory:
            request_path = Path(directory) / "scan-request.json"
            with (
                patch.object(app_module, "_repository", return_value=repository),
                patch.object(
                    app_module,
                    "_selected_discovery_subnets",
                    return_value=["192.168.40.0/24", "10.0.202.0/24"],
                ),
                patch.object(app_module, "_read_scan_state", return_value={"status": "idle"}),
                patch.object(app_module, "_decorate_adoptions", side_effect=lambda state: state),
                patch.object(app_module, "SCAN_REQUEST", request_path),
            ):
                response = app_module.start_discovery("scan")
                request = json.loads(request_path.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            request["subnets"],
            ["192.168.40.0/24", "10.0.202.0/24"],
        )
        payload = json.loads(response.body)
        self.assertEqual(
            [network["status"] for network in payload["networks"]],
            ["queued", "queued"],
        )

    def test_scan_requires_at_least_one_saved_subnet(self) -> None:
        with (
            patch.object(app_module, "_repository", return_value=Mock()),
            patch.object(app_module, "_selected_discovery_subnets", return_value=[]),
        ):
            response = app_module.start_discovery("scan")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(json.loads(response.body)["status"], "no_networks")


class FrigateTargetApiTests(unittest.TestCase):
    def test_frigate_error_exposes_safe_full_sync_context(self) -> None:
        response = app_module._frigate_target_error(
            app_module.FrigateApiError(
                "capability_unavailable",
                stage="remove_runtime_stream",
                resource="camadmiral_synthetic_stale_detect",
            )
        )

        payload = json.loads(response.body)
        self.assertEqual(payload["status"], "capability_unavailable")
        self.assertEqual(payload["stage"], "remove_runtime_stream")
        self.assertEqual(payload["resource"], "camadmiral_synthetic_stale_detect")
        self.assertNotIn("rtsp://", response.body.decode())

    def test_frigate_error_includes_safe_upstream_detail(self) -> None:
        response = app_module._frigate_target_error(
            app_module.FrigateApiError(
                "request_rejected",
                stage="remove_runtime_stream",
                resource="camadmiral_synthetic_stale_detect",
                upstream_status=400,
                upstream_detail="yaml: path not exist",
            )
        )

        payload = json.loads(response.body)
        self.assertIn("Frigate response: yaml: path not exist", payload["message"])

    def test_add_validates_and_persists_a_loopback_target(self) -> None:
        repository = Mock()
        repository.frigate_targets.return_value = []
        repository.frigate_target.return_value = {
            "target_id": "frigate-synthetic",
            "name": "Local Frigate",
            "api_url": "http://127.0.0.1:20001",
            "selected_cameras": 0,
            "connection_status": "connected",
        }
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "_check_frigate_target") as check,
            patch.object(app_module, "_queue_frigate_reconciliation") as queue,
        ):
            response = app_module.add_frigate_target(
                app_module.FrigateTargetRequest(
                    name="Local Frigate",
                    api_url="http://127.0.0.1:20001/",
                ),
                "add-frigate-target",
            )

        self.assertEqual(response.status_code, 201)
        check.assert_called_once()
        repository.save_frigate_target.assert_called_once()
        self.assertEqual(
            repository.save_frigate_target.call_args.args[2],
            "http://127.0.0.1:20001",
        )
        repository.record_frigate_target_check.assert_called_once()
        queue.assert_called_once()

    def test_add_accepts_an_operator_selected_remote_target(self) -> None:
        repository = Mock()
        repository.frigate_targets.return_value = []
        repository.frigate_target.return_value = {
            "target_id": "frigate-remote",
            "name": "Remote",
            "api_url": "http://192.0.2.10:5000",
            "selected_cameras": 0,
            "connection_status": "connected",
        }
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "_check_frigate_target") as check,
        ):
            response = app_module.add_frigate_target(
                app_module.FrigateTargetRequest(
                    name="Remote",
                    api_url="http://192.0.2.10:5000",
                ),
                "add-frigate-target",
            )

        self.assertEqual(response.status_code, 201)
        check.assert_called_once()
        repository.save_frigate_target.assert_called_once()
        self.assertEqual(
            repository.save_frigate_target.call_args.args[2],
            "http://192.0.2.10:5000",
        )

    def test_add_rejects_a_blank_name_before_network_access(self) -> None:
        repository = Mock()
        repository.frigate_targets.return_value = []
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "_check_frigate_target") as check,
        ):
            response = app_module.add_frigate_target(
                app_module.FrigateTargetRequest(
                    name="   ",
                    api_url="http://127.0.0.1:20001",
                ),
                "add-frigate-target",
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(json.loads(response.body)["status"], "invalid_name")
        check.assert_not_called()
        repository.save_frigate_target.assert_not_called()

    def test_connection_test_clears_restart_required_after_runtime_cleanup(self) -> None:
        repository = Mock()
        repository.frigate_target.return_value = {
            "target_id": "frigate-synthetic",
            "name": "Local Frigate",
            "api_url": "http://127.0.0.1:20001",
            "restart_recommended": True,
        }
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(
                app_module,
                "frigate_restart_required",
                return_value=False,
            ) as restart_required,
            patch.object(app_module, "_check_frigate_target") as basic_check,
        ):
            response = app_module.test_frigate_target(
                "frigate-synthetic",
                "test-frigate-target",
            )

        self.assertEqual(response.status_code, 200)
        restart_required.assert_called_once()
        basic_check.assert_not_called()
        repository.record_frigate_target_check.assert_called_once_with(
            "frigate-synthetic",
            status="connected",
            restart_recommended=False,
        )

    def test_target_address_choice_is_saved_without_changing_stream_preference(self) -> None:
        repository = Mock()
        repository.frigate_target.return_value = {
            "target_id": "frigate-synthetic",
            "name": "Local Frigate",
            "api_url": "http://127.0.0.1:20001",
            "address_mode": "localhost",
        }
        with patch.object(app_module, "_repository", return_value=repository):
            response = app_module.set_frigate_target_address(
                "frigate-synthetic",
                app_module.FrigateTargetAddressRequest(address_mode="localhost"),
                "set-frigate-target-address",
            )

        self.assertEqual(response.status_code, 200)
        repository.set_frigate_target_address_mode.assert_called_once_with(
            "frigate-synthetic", "localhost"
        )
        repository.set_camera_stream_address_mode.assert_not_called()

    def test_remove_leaves_frigate_configuration_untouched(self) -> None:
        repository = Mock()
        repository.remove_frigate_target.return_value = True
        with patch.object(app_module, "_repository", return_value=repository):
            response = app_module.remove_frigate_target(
                "frigate-synthetic",
                "remove-frigate-target",
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("left unchanged", json.loads(response.body)["message"])
        repository.remove_frigate_target.assert_called_once_with("frigate-synthetic")

    def test_full_sync_uses_one_confirmed_action_for_the_whole_target(self) -> None:
        repository = Mock()
        repository.frigate_target.return_value = {
            "target_id": "frigate-synthetic",
            "name": "Local Frigate",
            "api_url": "http://127.0.0.1:20001",
            "selected_cameras": 3,
        }
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "media_host_for_mode", return_value="192.168.50.12"),
            patch.object(
                app_module,
                "full_sync_frigate",
                return_value={
                    "removed_cameras": 2,
                    "removed_streams": 4,
                    "restart_recommended": True,
                    "applied": 3,
                    "pending": 0,
                },
            ) as full_sync,
        ):
            response = app_module.apply_full_sync(
                "frigate-synthetic",
                "full-sync-frigate-target",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["removed_cameras"], 2)
        full_sync.assert_called_once()
        repository.record_frigate_target_check.assert_called_once_with(
            "frigate-synthetic",
            status="connected",
            restart_recommended=True,
        )

    def test_full_sync_preview_returns_counts_without_resource_names(self) -> None:
        repository = Mock()
        repository.frigate_target.return_value = {
            "target_id": "frigate-synthetic",
            "name": "Local Frigate",
            "api_url": "http://127.0.0.1:20001",
            "selected_cameras": 3,
        }
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(
                app_module,
                "preview_frigate_full_sync",
                return_value={
                    "managed_cameras": 3,
                    "stale_cameras": ["camadmiral_old"],
                    "stale_streams": ["camadmiral_old_record", "camadmiral_old_detect"],
                },
            ),
        ):
            response = app_module.preview_full_sync("frigate-synthetic")

        payload = json.loads(response.body)
        self.assertEqual(payload["stale_cameras"], 1)
        self.assertEqual(payload["stale_streams"], 2)
        self.assertNotIn("camadmiral_old", response.body.decode())

    def test_camera_sync_selects_only_the_requested_camera(self) -> None:
        repository = Mock()
        repository.frigate_target.return_value = {
            "target_id": "frigate-synthetic",
            "name": "Local Frigate",
            "api_url": "http://127.0.0.1:20001",
            "selected_cameras": 0,
        }
        repository.camera.return_value = {"camera_uuid": "camera-1"}
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(
                app_module,
                "_queue_frigate_camera_reconciliation",
                return_value=True,
            ) as queue,
        ):
            response = app_module.sync_frigate_camera(
                "frigate-synthetic",
                "camera-1",
                "sync-frigate-camera",
                app_module.FrigateCameraSyncRequest(address_mode="localhost"),
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            json.loads(response.body),
            {"status": "syncing", "selected": True, "queued": True},
        )
        repository.select_frigate_camera.assert_called_once_with(
            "frigate-synthetic", "camera-1", "localhost"
        )
        repository.mark_frigate_binding_pending.assert_called_once_with(
            "frigate-synthetic", "camera-1"
        )
        queue.assert_called_once_with("frigate-synthetic", "camera-1")

    def test_camera_sync_without_body_preserves_existing_address_mode(self) -> None:
        repository = Mock()
        repository.frigate_target.return_value = {
            "target_id": "frigate-synthetic",
            "name": "Local Frigate",
            "api_url": "http://127.0.0.1:20001",
            "selected_cameras": 1,
        }
        repository.camera.return_value = {"camera_uuid": "camera-1"}
        repository.frigate_camera_address_mode.return_value = "localhost"
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(
                app_module,
                "_queue_frigate_camera_reconciliation",
                return_value=False,
            ),
        ):
            response = app_module.sync_frigate_camera(
                "frigate-synthetic",
                "camera-1",
                "sync-frigate-camera",
            )

        self.assertEqual(response.status_code, 202)
        self.assertFalse(json.loads(response.body)["queued"])
        repository.select_frigate_camera.assert_called_once_with(
            "frigate-synthetic", "camera-1", "localhost"
        )

    def test_camera_sync_job_reconciles_only_the_requested_camera(self) -> None:
        repository = Mock()
        repository.frigate_target.return_value = {
            "name": "Local Frigate",
            "api_url": "http://127.0.0.1:20001",
        }
        job = ("frigate-synthetic", "camera-1")
        with app_module.FRIGATE_CAMERA_JOBS_LOCK:
            app_module.FRIGATE_CAMERA_JOBS.add(job)
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "media_host_for_mode", return_value="192.0.2.10"),
            patch.object(app_module, "reconcile_frigate") as reconcile,
        ):
            app_module._sync_frigate_camera_job(*job)

        reconcile.assert_called_once()
        self.assertEqual(reconcile.call_args.kwargs["camera_uuid"], "camera-1")
        repository.record_frigate_target_check.assert_called_once_with(
            "frigate-synthetic", status="connected"
        )
        self.assertNotIn(job, app_module.FRIGATE_CAMERA_JOBS)

    def test_camera_sync_queue_deduplicates_an_active_camera_job(self) -> None:
        job = ("frigate-synthetic", "camera-1")
        thread = Mock()
        with app_module.FRIGATE_CAMERA_JOBS_LOCK:
            app_module.FRIGATE_CAMERA_JOBS.discard(job)
        try:
            with patch.object(app_module.threading, "Thread", return_value=thread):
                self.assertTrue(app_module._queue_frigate_camera_reconciliation(*job))
                self.assertFalse(app_module._queue_frigate_camera_reconciliation(*job))
            thread.start.assert_called_once_with()
        finally:
            with app_module.FRIGATE_CAMERA_JOBS_LOCK:
                app_module.FRIGATE_CAMERA_JOBS.discard(job)

    def test_camera_sync_queue_recovers_when_thread_cannot_start(self) -> None:
        job = ("frigate-synthetic", "camera-1")
        thread = Mock()
        thread.start.side_effect = RuntimeError("synthetic thread failure")
        with app_module.FRIGATE_CAMERA_JOBS_LOCK:
            app_module.FRIGATE_CAMERA_JOBS.discard(job)

        with (
            patch.object(app_module.threading, "Thread", return_value=thread),
            self.assertRaisesRegex(RuntimeError, "synthetic thread failure"),
        ):
            app_module._queue_frigate_camera_reconciliation(*job)

        self.assertNotIn(job, app_module.FRIGATE_CAMERA_JOBS)

    def test_camera_config_preview_returns_masked_and_copyable_versions(self) -> None:
        repository = Mock()
        repository.frigate_target.return_value = {
            "target_id": "frigate-synthetic",
            "name": "Local Frigate",
            "api_url": "http://127.0.0.1:20001",
            "selected_cameras": 0,
        }
        repository.camera.return_value = {"camera_uuid": "camera-1"}
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "media_host_for_mode", return_value="192.168.50.12"),
            patch.object(
                app_module,
                "frigate_camera_configuration",
                return_value={
                    "configuration": "source: rtsp://camadmiral:synthetic-secret@host/live",
                    "display_configuration": "source: rtsp://camadmiral:********@host/live",
                },
            ),
        ):
            response = app_module.preview_frigate_camera_config(
                "frigate-synthetic", "camera-1"
            )

        payload = json.loads(response.body)
        self.assertIn("synthetic-secret", payload["configuration"])
        self.assertNotIn("synthetic-secret", payload["display_configuration"])

    def test_camera_remove_cleans_only_the_selected_managed_camera(self) -> None:
        repository = Mock()
        repository.frigate_target.return_value = {
            "target_id": "frigate-synthetic",
            "name": "Local Frigate",
            "api_url": "http://127.0.0.1:20001",
            "selected_cameras": 1,
        }
        repository.camera.return_value = {"camera_uuid": "camera-1"}
        repository.selected_frigate_camera_uuids.return_value = ["camera-1"]
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(
                app_module,
                "remove_frigate_camera",
                return_value={
                    "removed_cameras": 1,
                    "removed_streams": 2,
                    "restart_recommended": True,
                },
            ) as remove,
        ):
            response = app_module.remove_synced_frigate_camera(
                "frigate-synthetic",
                "camera-1",
                "remove-frigate-camera",
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(json.loads(response.body)["selected"])
        remove.assert_called_once()
        repository.record_frigate_target_check.assert_called_once_with(
            "frigate-synthetic",
            status="connected",
            restart_recommended=True,
        )


class ConsumerApiTests(unittest.TestCase):
    @staticmethod
    def request(host: str = "camadmiral.invalid:18080") -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "path": "/api/v1/cameras",
                "query_string": b"",
                "headers": [(b"host", host.encode("ascii"))],
                "server": (host.split(":", 1)[0], 18080),
                "client": ("127.0.0.1", 32000),
            }
        )

    @staticmethod
    def payload(response) -> dict:
        return json.loads(response.body)

    def test_missing_or_invalid_bearer_token_is_rejected(self) -> None:
        with patch.object(app_module, "read_secret_file", return_value=b"synthetic-api-token"):
            missing = app_module.consumer_cameras(self.request(), None)
            invalid = app_module.consumer_cameras(self.request(), "Bearer wrong-token")

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(missing.headers["www-authenticate"], "Bearer")
        self.assertEqual(self.payload(invalid), {"detail": "Unauthorized"})

    def test_unconfigured_token_returns_service_unavailable(self) -> None:
        with patch.object(
            app_module,
            "read_secret_file",
            side_effect=app_module.SecretConfigurationError("synthetic failure"),
        ):
            response = app_module.consumer_cameras(self.request(), "Bearer any-token")

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("synthetic failure", response.body.decode())

    def test_authorized_response_contains_only_consumer_metadata(self) -> None:
        repository = Mock()
        repository.rtsp_access_password.return_value = "synthetic-media-secret"
        repository.consumer_inventory.return_value = [
            {
                "camera_uuid": "camera-1",
                "candidate_uuid": "candidate-1",
                "display_name": "Entrance",
                "streams": [
                    {
                        "stream_uuid": "stream-1",
                        "stream_key": "stream_synthetic",
                        "roles": ["detect", "record"],
                        "health_status": "healthy",
                        "video_codec": "h264",
                        "probed_width": 1280,
                        "probed_height": 720,
                        "probed_fps": 15.0,
                        "encoding": "H264",
                        "width": 1920,
                        "height": 1080,
                        "fps": 20.0,
                    }
                ],
            }
        ]
        inventory = {
            "devices": [
                {
                    "candidate_uuid": "candidate-1",
                    "onvif": {
                        "service_urls": ["http://192.0.2.20/onvif/device_service"]
                    },
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = Path(directory) / "inventory.json"
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            with (
                patch.object(app_module, "read_secret_file", return_value=b"synthetic-api-token"),
                patch.object(app_module, "_repository", return_value=repository),
                patch.object(app_module, "INVENTORY", inventory_path),
            ):
                response = app_module.consumer_cameras(
                    self.request(), "Bearer synthetic-api-token"
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.payload(response),
            {
                "api_version": "1",
                "cameras": [
                    {
                        "id": "camera-1",
                        "name": "Entrance",
                        "state": "online",
                        "onvif": {
                            "available": True,
                            "device_service_url": "http://192.0.2.20/onvif/device_service",
                        },
                        "streams": [
                            {
                                "id": "stream-1",
                                "roles": ["detect", "record"],
                                "state": "healthy",
                                "video": {
                                    "codec": "h264",
                                    "width": 1280,
                                    "height": 720,
                                    "fps": 15.0,
                                },
                                "downstream": {
                                    "protocol": "rtsp",
                                    "url": "rtsp://camadmiral.invalid:18554/stream_synthetic",
                                    "authentication": {
                                        "type": "username_password",
                                        "username": "camadmiral",
                                        "password": "synthetic-media-secret",
                                    },
                                },
                            }
                        ],
                    }
                ],
            },
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("private-source", response.body.decode())

    def test_disabled_camera_remains_listed_without_downstream_streams(self) -> None:
        repository = Mock()
        repository.rtsp_access_password.return_value = "synthetic-media-secret"
        repository.consumer_inventory.return_value = [
            {
                "camera_uuid": "camera-disabled",
                "candidate_uuid": "candidate-disabled",
                "display_name": "Disabled camera",
                "enabled": False,
                "streams": [
                    {
                        "stream_uuid": "stream-private",
                        "stream_key": "stream_private",
                        "roles": ["detect", "record"],
                        "health_status": "healthy",
                    }
                ],
            }
        ]
        with (
            patch.object(app_module, "read_secret_file", return_value=b"synthetic-api-token"),
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "_inventory_candidates", return_value={}),
        ):
            response = app_module.consumer_cameras(
                self.request(), "Bearer synthetic-api-token"
            )

        camera = self.payload(response)["cameras"][0]
        self.assertEqual(camera["state"], "disabled")
        self.assertEqual(camera["streams"], [])
        self.assertNotIn("stream_private", response.body.decode())


class CameraLifecycleEndpointTests(unittest.TestCase):
    @staticmethod
    def payload(response) -> dict:
        return json.loads(response.body)

    def test_rename_uses_stable_camera_identity_and_queues_frigate(self) -> None:
        repository = Mock()
        repository.update_camera_name.return_value = True
        repository.camera.return_value = {
            "camera_uuid": "camera-1",
            "candidate_uuid": "candidate-1",
            "display_name": "Renamed",
            "enabled": True,
        }
        repository.adoption_for_candidate.return_value = {
            "camera_uuid": "camera-1",
            "display_name": "Renamed",
            "enabled": True,
        }
        with patch.object(app_module, "_repository", return_value=repository), patch.object(
            app_module, "_queue_frigate_reconciliation"
        ) as queue:
            response = app_module.update_camera(
                "camera-1",
                app_module.CameraUpdateRequest(display_name=" Renamed "),
                "update-camera",
            )

        self.assertEqual(response.status_code, 200)
        repository.update_camera_name.assert_called_once_with("camera-1", "Renamed")
        queue.assert_called_once_with()

    def test_stream_address_choice_is_saved_without_syncing_frigate(self) -> None:
        repository = Mock()
        repository.set_camera_stream_address_mode.return_value = True
        with patch.object(app_module, "_repository", return_value=repository):
            response = app_module.set_camera_stream_address(
                "camera-1",
                app_module.CameraStreamAddressRequest(address_mode="localhost"),
                "set-camera-stream-address",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.payload(response),
            {"status": "updated", "address_mode": "localhost"},
        )
        repository.set_camera_stream_address_mode.assert_called_once_with(
            "camera-1", "localhost"
        )

    def test_disable_withdraws_media_and_keeps_camera_adopted(self) -> None:
        repository = Mock()
        repository.camera.return_value = {
            "camera_uuid": "camera-1",
            "candidate_uuid": "candidate-1",
            "enabled": True,
        }
        repository.adoption_for_candidate.return_value = {
            "camera_uuid": "camera-1",
            "enabled": False,
            "streams": [{"stream_uuid": "stream-1"}],
        }
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "_reconcile_media", return_value=True) as reconcile,
            patch.object(app_module, "_reconcile_frigate") as reconcile_frigate_now,
        ):
            response = app_module.set_camera_enabled(
                "camera-1",
                app_module.CameraEnabledRequest(enabled=False),
                "set-camera-enabled",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.payload(response)["status"], "disabled")
        repository.set_camera_enabled.assert_called_once_with("camera-1", False)
        reconcile.assert_called_once_with()
        reconcile_frigate_now.assert_called_once_with()

    def test_enable_validates_saved_streams_before_changing_state(self) -> None:
        repository = Mock()
        repository.camera.return_value = {
            "camera_uuid": "camera-1",
            "candidate_uuid": "candidate-1",
            "enabled": False,
        }
        with patch.object(app_module, "_repository", return_value=repository), patch.object(
            app_module,
            "_validate_saved_camera_sources",
            return_value=(False, "Saved camera streams are unavailable."),
        ):
            response = app_module.set_camera_enabled(
                "camera-1",
                app_module.CameraEnabledRequest(enabled=True),
                "set-camera-enabled",
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.payload(response)["status"], "camera_unavailable")
        repository.set_camera_enabled.assert_not_called()

    def test_rejected_replacement_credentials_are_not_saved(self) -> None:
        repository = Mock()
        repository.camera.return_value = {
            "camera_uuid": "camera-1",
            "candidate_uuid": "candidate-1",
            "enabled": True,
        }
        with patch.object(app_module, "_repository", return_value=repository), patch.object(
            app_module,
            "_validate_replacement_credentials",
            return_value=(False, "credentials_required", "Incorrect username or password."),
        ), patch.object(app_module, "_reconcile_media") as reconcile:
            response = app_module.update_camera_credentials(
                "camera-1",
                app_module.CameraCredentialRequest(
                    username="operator",
                    password="wrong-secret",
                ),
                "update-camera-credentials",
            )

        self.assertEqual(response.status_code, 401)
        repository.replace_camera_credentials.assert_not_called()
        reconcile.assert_not_called()

    def test_valid_replacement_credentials_restore_existing_streams(self) -> None:
        repository = Mock()
        repository.camera.return_value = {
            "camera_uuid": "camera-1",
            "candidate_uuid": "candidate-1",
            "enabled": True,
        }
        repository.adoption_for_candidate.return_value = {
            "camera_uuid": "camera-1",
            "candidate_uuid": "candidate-1",
            "enabled": True,
            "streams": [{"stream_uuid": "stream-1", "health_status": "healthy"}],
        }
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(
                app_module,
                "_validate_replacement_credentials",
                return_value=(True, "ok", ""),
            ),
            patch.object(app_module, "_reconcile_media", return_value=True) as reconcile,
        ):
            response = app_module.update_camera_credentials(
                "camera-1",
                app_module.CameraCredentialRequest(
                    username="new-user",
                    password="new-secret",
                ),
                "update-camera-credentials",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.payload(response)["status"], "credentials_updated")
        repository.replace_camera_credentials.assert_called_once_with(
            "camera-1", "new-user", "new-secret"
        )
        reconcile.assert_called_once_with()


class CredentialValidationTests(unittest.TestCase):
    def test_onvif_auth_failure_stops_before_rtsp_validation(self) -> None:
        repository = Mock()
        repository.camera.return_value = {
            "candidate_uuid": "candidate-1",
            "enabled": True,
        }
        repository.adoption_for_candidate.return_value = {
            "candidate_uuid": "candidate-1",
            "streams": [{"source_kind": "onvif"}],
            "role_tokens": {"record": "profile-1", "detect": "profile-1"},
        }
        with (
            patch.object(app_module, "_find_candidate", return_value=synthetic_candidate()),
            patch.object(
                app_module,
                "inspect_onvif_candidate",
                side_effect=OnvifInspectionError(
                    "credentials_required", "synthetic authentication failure"
                ),
            ),
            patch.object(app_module, "_probe_exact_sources") as probe,
        ):
            result = app_module._validate_replacement_credentials(
                repository,
                "camera-1",
                "operator",
                "wrong-secret",
            )

        self.assertEqual(result[0:2], (False, "credentials_required"))
        probe.assert_not_called()

    def test_rtsp_only_credentials_are_validated_against_saved_role_streams(self) -> None:
        repository = Mock()
        repository.camera.return_value = {
            "candidate_uuid": "candidate-1",
            "enabled": True,
        }
        repository.adoption_for_candidate.return_value = {
            "candidate_uuid": "candidate-1",
            "streams": [{"source_kind": "manual_rtsp"}],
            "role_tokens": {"record": "manual-1", "detect": "manual-1"},
        }
        repository.managed_stream_sources.return_value = [
            {"uri": "rtsp://192.0.2.20/live", "stream_uuid": "stream-1"}
        ]
        with patch.object(
            app_module,
            "_probe_exact_sources",
            return_value=[ProbeResult("ready", 20, "h264", None, 1280, 720, 15)],
        ) as probe:
            result = app_module._validate_replacement_credentials(
                repository,
                "camera-1",
                "operator",
                "new-secret",
            )

        self.assertEqual(result, (True, "ok", ""))
        repository.managed_stream_sources.assert_called_once_with(
            include_disabled=True,
            camera_uuid="camera-1",
            role_bound_only=True,
        )
        probe.assert_called_once_with(
            ["rtsp://192.0.2.20/live"],
            "operator",
            "new-secret",
        )


class SnapshotEndpointTests(unittest.TestCase):
    def test_snapshot_returns_no_store_jpeg_for_adopted_camera(self) -> None:
        repository = Mock()
        repository.preview_stream_for_camera.return_value = {
            "stream_uuid": "stream-1",
            "stream_key": "stream_synthetic",
            "health_status": "healthy",
        }
        jpeg = b"\xff\xd8\xffsynthetic\xff\xd9"
        with patch.object(app_module, "_repository", return_value=repository), patch.object(
            app_module, "snapshot_frame", return_value=jpeg
        ) as snapshot:
            response = app_module.camera_snapshot("camera-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "image/jpeg")
        self.assertEqual(response.body, jpeg)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("x-camadmiral-captured-at", response.headers)
        snapshot.assert_called_once_with("stream_synthetic")

    def test_thumbnail_returns_only_an_already_cached_frame(self) -> None:
        jpeg = b"\xff\xd8\xffcached\xff\xd9"
        frame = app_module.RELAY_HEALTH_MONITOR.cache_frame("camera-cached", jpeg)

        response = app_module.camera_thumbnail("camera-cached")
        missing = app_module.camera_thumbnail("camera-missing-thumbnail")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, jpeg)
        self.assertEqual(
            response.headers["x-camadmiral-captured-at"],
            datetime.fromtimestamp(frame.captured_at, timezone.utc).isoformat(),
        )
        self.assertEqual(missing.status_code, 404)

    def test_snapshot_does_not_resolve_an_unknown_camera(self) -> None:
        repository = Mock()
        repository.preview_stream_for_camera.return_value = None
        with patch.object(app_module, "_repository", return_value=repository), patch.object(
            app_module, "snapshot_frame"
        ) as snapshot:
            response = app_module.camera_snapshot("camera-missing")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.body, b"")
        snapshot.assert_not_called()

    def test_snapshot_failure_returns_empty_unavailable_response(self) -> None:
        repository = Mock()
        repository.preview_stream_for_camera.return_value = {
            "stream_key": "stream_synthetic"
        }
        with patch.object(app_module, "_repository", return_value=repository), patch.object(
            app_module,
            "snapshot_frame",
            side_effect=app_module.SnapshotError("sensitive upstream detail"),
        ):
            response = app_module.camera_snapshot("camera-1")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.body, b"")
        self.assertNotIn(b"sensitive", response.body)


class LiveEndpointBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_live_control_allows_only_bounded_viewer_modes(self) -> None:
        self.assertEqual(
            app_module._live_control_message(
                json.dumps({"type": "mse", "value": "avc1.640029", "ignored": "field"})
            ),
            '{"type":"mse","value":"avc1.640029"}',
        )
        self.assertEqual(app_module._live_control_message('{"type":"mjpeg"}'), '{"type":"mjpeg"}')
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            app_module._live_control_message('{"type":"webrtc/offer","value":"synthetic"}')
        with self.assertRaisesRegex(ValueError, "too large"):
            app_module._live_control_message("x" * 4097)

    def test_live_origin_must_match_request_host(self) -> None:
        websocket = Mock()
        websocket.headers = {"origin": "http://camera-box.invalid", "host": "camera-box.invalid"}
        self.assertTrue(app_module._same_websocket_origin(websocket))
        websocket.headers["origin"] = "https://untrusted.invalid"
        self.assertFalse(app_module._same_websocket_origin(websocket))

    async def test_unknown_camera_is_rejected_before_relay_connection(self) -> None:
        websocket = Mock()
        websocket.headers = {"origin": "http://camera-box.invalid", "host": "camera-box.invalid"}
        websocket.close = AsyncMock()
        repository = Mock()
        repository.preview_stream_for_camera.return_value = None
        with patch.object(app_module, "_repository", return_value=repository), patch.object(
            app_module, "websocket_connect"
        ) as connect:
            await app_module.camera_live(websocket, "camera-missing")

        websocket.close.assert_awaited_once_with(code=4404)
        connect.assert_not_called()


class MediaHealthCycleTests(unittest.TestCase):
    def test_due_cycle_waits_for_media_lock_instead_of_being_discarded(self) -> None:
        repository = Mock()
        repository_ready = threading.Event()
        events = []

        def load_repository():
            repository_ready.set()
            return repository

        with (
            patch.object(app_module, "_repository", side_effect=load_repository),
            patch.object(
                app_module.RELAY_HEALTH_MONITOR,
                "probe",
                side_effect=lambda _repository: events.append("probe"),
            ) as probe,
            patch.object(
                app_module,
                "_queue_targeted_recovery_scan",
                side_effect=lambda _repository: events.append("queue") or False,
            ) as queue_recovery,
            patch.object(
                app_module,
                "_observe_inventory_identities",
                side_effect=lambda _repository: events.append("observe"),
            ) as observe_identities,
        ):
            app_module.MEDIA_LOCK.acquire()
            worker = threading.Thread(target=app_module._media_health_cycle)
            try:
                worker.start()
                self.assertTrue(repository_ready.wait(timeout=1))
                self.assertTrue(worker.is_alive())
                probe.assert_not_called()
            finally:
                app_module.MEDIA_LOCK.release()
            worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        probe.assert_called_once_with(repository)
        self.assertEqual(queue_recovery.call_count, 2)
        queue_recovery.assert_called_with(repository)
        observe_identities.assert_called_once_with(repository)
        self.assertEqual(events, ["queue", "probe", "queue", "observe"])

    def test_recovery_scheduler_does_not_skip_the_health_probe(self) -> None:
        repository = Mock()
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(
                app_module.RELAY_RUNTIME_ACTIVITY_MONITOR,
                "poll",
                return_value={"camera-stalled"},
            ) as runtime_poll,
            patch.object(
                app_module,
                "_queue_targeted_recovery_scan",
                side_effect=[True, False, False],
            ) as queue_recovery,
            patch.object(app_module.RELAY_HEALTH_MONITOR, "probe") as probe,
            patch.object(app_module, "_observe_inventory_identities") as observe,
        ):
            self.assertTrue(app_module._targeted_recovery_cycle())
            self.assertTrue(app_module._media_health_cycle())

        self.assertEqual(
            queue_recovery.call_args_list,
            [
                call(
                    repository,
                    runtime_stalled_camera_uuids={"camera-stalled"},
                ),
                call(repository),
                call(repository),
            ],
        )
        runtime_poll.assert_called_once_with(repository)
        probe.assert_called_once_with(repository)
        observe.assert_called_once_with(repository)

    def test_recovery_scheduler_skips_full_inventory_when_runtime_is_healthy(
        self,
    ) -> None:
        repository = Mock()
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(
                app_module.RELAY_RUNTIME_ACTIVITY_MONITOR,
                "poll",
                return_value=set(),
            ) as runtime_poll,
            patch.object(app_module, "_queue_targeted_recovery_scan") as queue_recovery,
            patch.object(app_module, "RELAY_RUNTIME_POLL_FAILURE_ACTIVE", False),
        ):
            self.assertFalse(app_module._targeted_recovery_cycle())

        runtime_poll.assert_called_once_with(repository)
        queue_recovery.assert_not_called()
        repository.adoption_map.assert_not_called()

    def test_recovery_scheduler_clears_failed_poll_and_logs_once_per_outage(
        self,
    ) -> None:
        repository = Mock()
        runtime_monitor = Mock()
        runtime_monitor.poll.side_effect = [
            RuntimeError("synthetic outage"),
            RuntimeError("same synthetic outage"),
            set(),
            RuntimeError("new synthetic outage"),
        ]
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "RELAY_RUNTIME_ACTIVITY_MONITOR", runtime_monitor),
            patch.object(app_module, "RELAY_RUNTIME_POLL_FAILURE_ACTIVE", False),
            patch.object(app_module, "_queue_targeted_recovery_scan") as queue_recovery,
            patch("builtins.print") as print_mock,
        ):
            self.assertFalse(app_module._targeted_recovery_cycle())
            self.assertFalse(app_module._targeted_recovery_cycle())
            self.assertFalse(app_module._targeted_recovery_cycle())
            self.assertFalse(app_module._targeted_recovery_cycle())

        self.assertEqual(runtime_monitor.reset.call_count, 3)
        self.assertEqual(print_mock.call_count, 2)
        print_mock.assert_called_with(
            "media: runtime activity poll failed (RuntimeError)",
            flush=True,
        )
        queue_recovery.assert_not_called()
        repository.adoption_map.assert_not_called()

    def test_recovery_scheduler_does_not_wait_for_the_media_lock(self) -> None:
        repository = Mock()
        media_lock = Mock()
        with (
            patch.object(app_module, "_repository", return_value=repository),
            patch.object(app_module, "MEDIA_LOCK", media_lock),
            patch.object(
                app_module.RELAY_RUNTIME_ACTIVITY_MONITOR,
                "poll",
                return_value={"camera-stalled"},
            ),
            patch.object(
                app_module,
                "_queue_targeted_recovery_scan",
                return_value=True,
            ) as queue_recovery,
        ):
            self.assertTrue(app_module._targeted_recovery_cycle())

        media_lock.acquire.assert_not_called()
        media_lock.release.assert_not_called()
        queue_recovery.assert_called_once_with(
            repository,
            runtime_stalled_camera_uuids={"camera-stalled"},
        )


class TargetedRecoveryScanTests(unittest.TestCase):
    def setUp(self) -> None:
        app_module.RECOVERY_SCAN_ATTEMPTS.clear()
        app_module.RECOVERY_SCAN_ATTEMPT_COUNTS.clear()

    def tearDown(self) -> None:
        app_module.RECOVERY_SCAN_ATTEMPTS.clear()
        app_module.RECOVERY_SCAN_ATTEMPT_COUNTS.clear()

    def test_first_failed_probe_queues_recovery_before_camera_is_marked_offline(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {
            "candidate-early": {
                "camera_uuid": "camera-early",
                "streams": [
                    {
                        "health_status": "healthy",
                        "probe_status": "unavailable",
                        "consecutive_failures": 1,
                    }
                ],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.json"
            request = root / "request.json"
            inventory.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "candidate_uuid": "candidate-early",
                                "mac": "02:00:00:00:00:41",
                                "onvif": {
                                    "endpoint_reference": "urn:uuid:synthetic-early-camera"
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            app_module.RECOVERY_SCAN_ATTEMPTS.clear()
            with (
                patch.object(app_module, "INVENTORY", inventory),
                patch.object(app_module, "SCAN_REQUEST", request),
                patch.object(app_module, "_read_scan_state", return_value={"status": "complete"}),
                patch.object(
                    app_module,
                    "_selected_discovery_subnets",
                    return_value=["172.22.41.0/24"],
                ),
            ):
                queued = app_module._queue_targeted_recovery_scan(repository)

        self.assertTrue(queued)

    def test_runtime_stall_queues_recovery_with_stale_healthy_inventory(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {
            "candidate-runtime": {
                "camera_uuid": "camera-runtime",
                "streams": [
                    {
                        "health_status": "healthy",
                        "probe_status": "ready",
                    }
                ],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.json"
            request = root / "request.json"
            inventory.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "candidate_uuid": "candidate-runtime",
                                "status": "online",
                                "mac": "02:00:00:00:00:45",
                                "onvif": {
                                    "endpoint_reference": "urn:uuid:synthetic-runtime-camera"
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(app_module, "INVENTORY", inventory),
                patch.object(app_module, "SCAN_REQUEST", request),
                patch.object(
                    app_module,
                    "_read_scan_state",
                    return_value={"status": "complete"},
                ),
                patch.object(
                    app_module,
                    "_selected_discovery_subnets",
                    return_value=["172.22.45.0/24"],
                ),
                patch.object(
                    app_module,
                    "RELAY_RUNTIME_ACTIVITY_MONITOR",
                    Mock(stalled_camera_uuids={"camera-runtime"}),
                ),
                patch.object(app_module.time, "monotonic", return_value=100.0),
            ):
                queued = app_module._queue_targeted_recovery_scan(repository)

        self.assertTrue(queued)
        self.assertEqual(
            app_module.RECOVERY_SCAN_ATTEMPT_COUNTS["candidate-runtime"],
            1,
        )

    def test_ready_probe_rearms_recovery_after_an_outage(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {
            "candidate-ready": {
                "camera_uuid": "camera-ready",
                "streams": [
                    {
                        "health_status": "healthy",
                        "probe_status": "ready",
                        "consecutive_failures": 0,
                    }
                ],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.json"
            request = root / "request.json"
            inventory.write_text(
                json.dumps({"devices": [{"candidate_uuid": "candidate-ready"}]}),
                encoding="utf-8",
            )
            app_module.RECOVERY_SCAN_ATTEMPTS.clear()
            app_module.RECOVERY_SCAN_ATTEMPTS["candidate-ready"] = 10.0
            app_module.RECOVERY_SCAN_ATTEMPT_COUNTS["candidate-ready"] = 3
            with (
                patch.object(app_module, "INVENTORY", inventory),
                patch.object(app_module, "SCAN_REQUEST", request),
                patch.object(app_module, "_read_scan_state", return_value={"status": "complete"}),
            ):
                queued = app_module._queue_targeted_recovery_scan(repository)

        self.assertFalse(queued)
        self.assertNotIn("candidate-ready", app_module.RECOVERY_SCAN_ATTEMPTS)
        self.assertNotIn("candidate-ready", app_module.RECOVERY_SCAN_ATTEMPT_COUNTS)

    def test_outage_gets_four_fast_retries_before_normal_backoff(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {
            "candidate-retry": {
                "camera_uuid": "camera-retry",
                "streams": [
                    {
                        "health_status": "degraded",
                        "probe_status": "unavailable",
                        "consecutive_failures": 2,
                    }
                ],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.json"
            request = root / "request.json"
            inventory.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "candidate_uuid": "candidate-retry",
                                "mac": "02:00:00:00:00:42",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            now = 100.0
            with (
                patch.object(app_module, "INVENTORY", inventory),
                patch.object(app_module, "SCAN_REQUEST", request),
                patch.object(app_module, "_read_scan_state", return_value={"status": "complete"}),
                patch.object(
                    app_module,
                    "_selected_discovery_subnets",
                    return_value=["172.22.42.0/24"],
                ),
                patch.object(app_module, "RECOVERY_SCAN_INTERVAL", 300.0),
            ):
                for expected_count in range(1, 6):
                    with patch.object(app_module.time, "monotonic", return_value=now):
                        self.assertTrue(app_module._queue_targeted_recovery_scan(repository))
                    self.assertEqual(
                        app_module.RECOVERY_SCAN_ATTEMPT_COUNTS["candidate-retry"],
                        expected_count,
                    )
                    request.unlink()
                    now += app_module.RECOVERY_SCAN_FAST_RETRY_INTERVAL

                with patch.object(app_module.time, "monotonic", return_value=now):
                    self.assertFalse(app_module._queue_targeted_recovery_scan(repository))
                with patch.object(
                    app_module.time,
                    "monotonic",
                    return_value=100.0
                    + 4 * app_module.RECOVERY_SCAN_FAST_RETRY_INTERVAL
                    + 300.0,
                ):
                    self.assertTrue(app_module._queue_targeted_recovery_scan(repository))

    def test_recovery_batches_prioritize_cameras_not_yet_attempted(self) -> None:
        candidate_uuids = [f"candidate-{index:02d}" for index in range(17)]
        repository = Mock()
        repository.adoption_map.return_value = {
            candidate_uuid: {
                "camera_uuid": f"camera-{index:02d}",
                "streams": [
                    {
                        "health_status": "degraded",
                        "probe_status": "unavailable",
                    }
                ],
            }
            for index, candidate_uuid in enumerate(candidate_uuids)
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.json"
            request = root / "request.json"
            inventory.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "candidate_uuid": candidate_uuid,
                                "mac": f"02:00:00:00:01:{index:02x}",
                            }
                            for index, candidate_uuid in enumerate(candidate_uuids)
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(app_module, "INVENTORY", inventory),
                patch.object(app_module, "SCAN_REQUEST", request),
                patch.object(app_module, "_read_scan_state", return_value={"status": "complete"}),
                patch.object(
                    app_module,
                    "_selected_discovery_subnets",
                    return_value=["172.22.43.0/24"],
                ),
            ):
                with patch.object(app_module.time, "monotonic", return_value=100.0):
                    self.assertTrue(app_module._queue_targeted_recovery_scan(repository))
                first = json.loads(request.read_text(encoding="utf-8"))
                request.unlink()
                with patch.object(app_module.time, "monotonic", return_value=110.0):
                    self.assertTrue(app_module._queue_targeted_recovery_scan(repository))
                second = json.loads(request.read_text(encoding="utf-8"))

        self.assertEqual(len(first["targets"]), 16)
        self.assertNotIn(
            candidate_uuids[-1],
            {target["candidate_uuid"] for target in first["targets"]},
        )
        self.assertIn(
            candidate_uuids[-1],
            {target["candidate_uuid"] for target in second["targets"]},
        )

    def test_offline_camera_queues_identity_only_targeted_scan(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {
            "candidate-1": {
                "camera_uuid": "camera-1",
                "streams": [{"health_status": "offline"}],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.json"
            request = root / "request.json"
            inventory.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "candidate_uuid": "candidate-1",
                                "mac": "02:00:00:00:00:01",
                                "onvif": {"endpoint_reference": "urn:uuid:synthetic-camera"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            app_module.RECOVERY_SCAN_ATTEMPTS.clear()
            with (
                patch.object(app_module, "INVENTORY", inventory),
                patch.object(app_module, "SCAN_REQUEST", request),
                patch.object(app_module, "_read_scan_state", return_value={"status": "complete"}),
                patch.object(
                    app_module,
                    "_selected_discovery_subnets",
                    return_value=["172.20.0.0/16", "172.20.37.0/24"],
                ),
                patch.object(app_module.time, "monotonic", return_value=10.0),
            ):
                queued = app_module._queue_targeted_recovery_scan(repository)
                payload = json.loads(request.read_text(encoding="utf-8"))

        self.assertTrue(queued)
        self.assertEqual(payload["mode"], "targeted")
        self.assertEqual(
            payload["subnets"],
            ["172.20.0.0/16", "172.20.37.0/24"],
        )
        self.assertEqual(
            payload["targets"],
            [
                {
                    "candidate_uuid": "candidate-1",
                    "endpoint_reference": "urn:uuid:synthetic-camera",
                    "mac": "02:00:00:00:00:01",
                }
            ],
        )
        self.assertNotIn("password", json.dumps(payload).lower())

    def test_offline_camera_retries_quickly_after_a_missed_scan(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {
            "candidate-retry": {
                "camera_uuid": "camera-retry",
                "streams": [
                    {
                        "health_status": "healthy",
                        "probe_status": "ready",
                    }
                ],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.json"
            request = root / "request.json"
            inventory.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "candidate_uuid": "candidate-retry",
                                "status": "offline",
                                "mac": "02:00:00:00:00:03",
                                "onvif": {
                                    "endpoint_reference": "urn:uuid:synthetic-retry-camera"
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            app_module.RECOVERY_SCAN_ATTEMPTS["candidate-retry"] = 100.0
            app_module.RECOVERY_SCAN_ATTEMPT_COUNTS["candidate-retry"] = 1
            with (
                patch.object(app_module, "INVENTORY", inventory),
                patch.object(app_module, "SCAN_REQUEST", request),
                patch.object(app_module, "_read_scan_state", return_value={"status": "complete"}),
                patch.object(
                    app_module,
                    "_selected_discovery_subnets",
                    return_value=["172.22.44.0/24"],
                ),
                patch.object(app_module, "RECOVERY_SCAN_INTERVAL", 300.0),
                patch.object(
                    app_module.time,
                    "monotonic",
                    side_effect=[
                        100.0 + app_module.RECOVERY_SCAN_FAST_RETRY_INTERVAL - 1.0,
                        100.0 + app_module.RECOVERY_SCAN_FAST_RETRY_INTERVAL,
                    ],
                ),
            ):
                queued_early = app_module._queue_targeted_recovery_scan(repository)
                queued = app_module._queue_targeted_recovery_scan(repository)

        self.assertFalse(queued_early)
        self.assertTrue(queued)
        self.assertEqual(
            app_module.RECOVERY_SCAN_ATTEMPT_COUNTS["candidate-retry"],
            2,
        )

    def test_auth_failed_camera_still_queues_identity_recovery(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {
            "candidate-auth": {
                "camera_uuid": "camera-auth",
                "streams": [
                    {"health_status": "auth_failed", "probe_status": "auth_failed"}
                ],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.json"
            request = root / "request.json"
            inventory.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "candidate_uuid": "candidate-auth",
                                "mac": "02:00:00:00:00:02",
                                "onvif": {
                                    "endpoint_reference": "urn:uuid:synthetic-auth-camera"
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            app_module.RECOVERY_SCAN_ATTEMPTS.clear()
            with (
                patch.object(app_module, "INVENTORY", inventory),
                patch.object(app_module, "SCAN_REQUEST", request),
                patch.object(app_module, "_read_scan_state", return_value={"status": "complete"}),
                patch.object(
                    app_module,
                    "_selected_discovery_subnets",
                    return_value=["172.21.10.0/24"],
                ),
                patch.object(app_module, "RECOVERY_SCAN_INTERVAL", 300.0),
            ):
                with patch.object(app_module.time, "monotonic", return_value=100.0):
                    queued = app_module._queue_targeted_recovery_scan(repository)
                payload = json.loads(request.read_text(encoding="utf-8"))
                request.unlink()
                with patch.object(app_module.time, "monotonic", return_value=110.0):
                    fast_retry = app_module._queue_targeted_recovery_scan(repository)

        self.assertTrue(queued)
        self.assertFalse(fast_retry)
        self.assertEqual(payload["targets"][0]["candidate_uuid"], "candidate-auth")

    def test_conflicted_camera_does_not_queue_targeted_recovery(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {
            "candidate-conflict": {
                "camera_uuid": "camera-conflict",
                "streams": [{"health_status": "offline"}],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.json"
            request = root / "request.json"
            inventory.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "candidate_uuid": "candidate-conflict",
                                "mac": "02:00:00:00:00:31",
                                "onvif": {
                                    "endpoint_reference": "urn:uuid:synthetic-conflict"
                                },
                                "identity_conflict": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            app_module.RECOVERY_SCAN_ATTEMPTS.clear()
            with (
                patch.object(app_module, "INVENTORY", inventory),
                patch.object(app_module, "SCAN_REQUEST", request),
                patch.object(app_module, "_read_scan_state", return_value={"status": "complete"}),
                patch.object(app_module, "_selected_discovery_subnets") as selected_subnets,
            ):
                queued = app_module._queue_targeted_recovery_scan(repository)

        self.assertFalse(queued)
        self.assertFalse(request.exists())
        selected_subnets.assert_not_called()

    def test_offline_camera_is_not_queued_without_a_selected_subnet(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {
            "candidate-1": {
                "camera_uuid": "camera-1",
                "streams": [{"health_status": "offline"}],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.json"
            request = root / "request.json"
            inventory.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "candidate_uuid": "candidate-1",
                                "onvif": {
                                    "endpoint_reference": "urn:uuid:synthetic-camera"
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            app_module.RECOVERY_SCAN_ATTEMPTS.clear()
            with (
                patch.object(app_module, "INVENTORY", inventory),
                patch.object(app_module, "SCAN_REQUEST", request),
                patch.object(app_module, "_read_scan_state", return_value={"status": "complete"}),
                patch.object(app_module, "_selected_discovery_subnets", return_value=[]),
            ):
                queued = app_module._queue_targeted_recovery_scan(repository)

        self.assertFalse(queued)
        self.assertFalse(request.exists())

    def test_competing_scan_consumed_before_lock_is_not_overwritten(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {
            "candidate-race": {
                "camera_uuid": "camera-race",
                "streams": [{"health_status": "offline"}],
            }
        }

        class CompetingScanLock:
            def __init__(self, scan_state: Path) -> None:
                self.scan_state = scan_state

            def __enter__(self):
                self.scan_state.write_text(
                    json.dumps({"status": "running", "scan_id": "competing-scan"}),
                    encoding="utf-8",
                )
                return self

            def __exit__(self, _exc_type, _exc, _traceback) -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.json"
            request = root / "request.json"
            scan_state = root / "scan-state.json"
            inventory.write_text(
                json.dumps(
                    {
                        "devices": [
                            {
                                "candidate_uuid": "candidate-race",
                                "status": "offline",
                                "mac": "02:00:00:00:00:46",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            scan_state.write_text(json.dumps({"status": "complete"}), encoding="utf-8")
            with (
                patch.object(app_module, "INVENTORY", inventory),
                patch.object(app_module, "SCAN_REQUEST", request),
                patch.object(app_module, "SCAN_STATE", scan_state),
                patch.object(
                    app_module,
                    "SCAN_REQUEST_LOCK",
                    CompetingScanLock(scan_state),
                ),
                patch.object(
                    app_module,
                    "_selected_discovery_subnets",
                    return_value=["172.22.46.0/24"],
                ),
            ):
                queued = app_module._queue_targeted_recovery_scan(repository)

        self.assertFalse(queued)
        self.assertFalse(request.exists())
        self.assertNotIn("candidate-race", app_module.RECOVERY_SCAN_ATTEMPTS)
        self.assertNotIn("candidate-race", app_module.RECOVERY_SCAN_ATTEMPT_COUNTS)

class RtspAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = FakeRepository()
        self.candidate = {
            "candidate_uuid": "candidate-1",
            "display_name": "Legacy camera",
            "ip": "192.168.10.20",
            "status": "online",
            "rtsp": [{"url": "rtsp://192.168.10.20:554/"}],
        }

    def invoke(self, request: app_module.RtspAdoptionRequest):
        with patch.object(app_module, "_find_candidate", return_value=self.candidate), patch.object(
            app_module, "_repository", return_value=self.repository
        ), patch.object(app_module, "_reconcile_media"):
            return app_module.adopt_rtsp_candidate("candidate-1", request, "adopt-rtsp")

    @staticmethod
    def payload(response) -> dict:
        return json.loads(response.body)

    def test_exact_rtsp_source_is_probed_then_adopted(self) -> None:
        request = app_module.RtspAdoptionRequest(
            display_name="Entrance",
            username="operator",
            password="synthetic-secret",
            sources=[
                app_module.RtspSourceRequest(
                    label="Primary",
                    url="rtsp://192.168.10.20:554/live?channel=1",
                )
            ],
        )
        with patch.object(
            app_module,
            "probe_source",
            return_value=ProbeResult("ready", 120, "h264", "aac", 1280, 720, 15),
        ) as probe:
            response = self.invoke(request)

        self.assertEqual(response.status_code, 200)
        payload = self.payload(response)
        self.assertEqual(payload["status"], "adopted")
        self.assertEqual(payload["profiles"][0]["source_kind"], "manual_rtsp")
        self.assertEqual(payload["role_tokens"]["record"], payload["role_tokens"]["detect"])
        probe.assert_called_once_with(
            "rtsp://192.168.10.20:554/live?channel=1",
            "operator",
            "synthetic-secret",
        )
        self.assertEqual(self.repository.saved_credentials, ("operator", "synthetic-secret"))

    def test_rtsp_source_cannot_target_another_host(self) -> None:
        request = app_module.RtspAdoptionRequest(
            sources=[app_module.RtspSourceRequest(url="rtsp://192.168.10.99/live")]
        )

        response = self.invoke(request)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.payload(response)["status"], "invalid_source")
        self.assertIsNone(self.repository.saved_credentials)

    def test_unreadable_rtsp_source_is_not_persisted(self) -> None:
        request = app_module.RtspAdoptionRequest(
            sources=[app_module.RtspSourceRequest(url="rtsp://192.168.10.20/live")]
        )
        with patch.object(
            app_module,
            "probe_source",
            return_value=ProbeResult("unavailable", 80),
        ):
            response = self.invoke(request)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.payload(response)["status"], "media_unavailable")
        self.assertIsNone(self.repository.saved_credentials)

    def test_catalog_paths_are_probed_and_successful_sources_are_persisted(self) -> None:
        request = app_module.RtspAdoptionRequest(
            display_name="Entrance",
            username="operator",
            password="synthetic-secret",
        )
        candidates = [
            CatalogCandidate(
                "Main stream",
                "rtsp://192.168.10.20/main",
                "synthetic-rule",
                "synthetic-revision",
                "https://vendor.invalid/official-documentation",
            ),
            CatalogCandidate(
                "Sub stream",
                "rtsp://192.168.10.20/sub",
                "synthetic-rule",
                "synthetic-revision",
                "https://vendor.invalid/official-documentation",
            ),
        ]
        with patch.object(app_module, "catalog_candidates", return_value=candidates), patch.object(
            app_module,
            "probe_source",
            side_effect=[
                ProbeResult("ready", 40, "h264", None, 1920, 1080, 20),
                ProbeResult("ready", 45, "h264", None, 640, 360, 10),
            ],
        ) as probe:
            response = self.invoke(request)

        payload = self.payload(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["catalog_revision"], "synthetic-revision")
        self.assertEqual({profile["source_kind"] for profile in payload["profiles"]}, {"catalog_rtsp"})
        self.assertEqual(probe.call_count, 2)

    def test_catalog_stops_after_first_authentication_failure(self) -> None:
        request = app_module.RtspAdoptionRequest(
            username="operator",
            password="incorrect-secret",
        )
        candidates = [
            CatalogCandidate(
                "Main stream",
                "rtsp://192.168.10.20/main",
                "synthetic-rule",
                "synthetic-revision",
                "https://vendor.invalid/official-documentation",
            ),
            CatalogCandidate(
                "Sub stream",
                "rtsp://192.168.10.20/sub",
                "synthetic-rule",
                "synthetic-revision",
                "https://vendor.invalid/official-documentation",
            ),
        ]
        with patch.object(app_module, "catalog_candidates", return_value=candidates), patch.object(
            app_module,
            "probe_source",
            return_value=ProbeResult("auth_failed", 30),
        ) as probe:
            response = self.invoke(request)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.payload(response)["status"], "credentials_required")
        probe.assert_called_once()
        self.assertIsNone(self.repository.saved_credentials)


class AdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = FakeRepository()

    def invoke(self, request: app_module.AdoptionRequest, *, amcrest: bool = True):
        with patch.object(app_module, "_find_candidate", return_value=synthetic_candidate(amcrest=amcrest)), patch.object(
            app_module, "_repository", return_value=self.repository
        ), patch.object(app_module, "_reconcile_media"), patch.object(
            app_module, "select_stream_roles", return_value={"record": "profile-1", "detect": "profile-1"}
        ):
            return app_module.adopt_candidate("candidate-1", request, "adopt")

    @staticmethod
    def payload(response) -> dict:
        return json.loads(response.body)

    def test_factory_credentials_require_confirmation_for_any_brand(self) -> None:
        calls = []

        def inspect(_candidate, *, username=None, password=None):
            calls.append((username, password))
            if (username, password) == ("admin", "admin"):
                return inspection()
            raise OnvifInspectionError("credentials_required", "Credentials required")

        with patch.object(app_module, "inspect_onvif_candidate", side_effect=inspect):
            response = self.invoke(
                app_module.AdoptionRequest(username="admin", password="new-secret"),
                amcrest=False,
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.payload(response)["status"], "factory_credentials_available")
        self.assertEqual(calls, [("admin", "new-secret"), ("admin", "admin")])
        self.assertIsNone(self.repository.saved_credentials)

    def test_confirmed_factory_credentials_are_stored_and_adopted(self) -> None:
        with patch.object(app_module, "inspect_onvif_candidate", return_value=inspection()) as inspect:
            response = self.invoke(
                app_module.AdoptionRequest(
                    username="admin",
                    password="new-secret",
                    allow_factory_credentials=True,
                ),
                amcrest=False,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.payload(response)["status"], "adopted")
        inspect.assert_called_once_with(
            synthetic_candidate(amcrest=False),
            username="admin",
            password="admin",
        )
        self.assertEqual(self.repository.saved_credentials, ("admin", "admin"))

    def test_explicit_admin_admin_still_requires_confirmation(self) -> None:
        with patch.object(app_module, "inspect_onvif_candidate", return_value=inspection()):
            response = self.invoke(app_module.AdoptionRequest(username="admin", password="admin"))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.payload(response)["status"], "factory_credentials_available")
        self.assertIsNone(self.repository.saved_credentials)

    def test_rejected_factory_credentials_preserve_original_auth_error(self) -> None:
        calls = []

        def reject(_candidate, *, username=None, password=None):
            calls.append((username, password))
            raise OnvifInspectionError("credentials_required", "Credentials required")

        with patch.object(app_module, "inspect_onvif_candidate", side_effect=reject):
            response = self.invoke(
                app_module.AdoptionRequest(username="operator", password="incorrect-secret"),
                amcrest=False,
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.payload(response)["status"], "credentials_required")
        self.assertEqual(calls, [("operator", "incorrect-secret"), ("admin", "admin")])
        self.assertIsNone(self.repository.saved_credentials)


if __name__ == "__main__":
    unittest.main()
