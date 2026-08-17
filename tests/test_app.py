import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from starlette.requests import Request

from camadmiral import app as app_module
from camadmiral.frigate import FrigateTarget
from camadmiral.onvif_client import OnvifInspectionError
from camadmiral.media import ProbeResult
from camadmiral.rtsp_catalog import CatalogCandidate


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


class DiscoveryDecorationTests(unittest.TestCase):
    def test_index_csp_allows_in_memory_player_media_only(self) -> None:
        response = app_module.index()
        policy = response.headers["content-security-policy"]
        self.assertIn("media-src 'self' blob:", policy)
        self.assertIn("img-src 'self' data: blob:", policy)
        self.assertEqual(policy.count("blob:"), 2)

    def test_adopted_name_replaces_scanner_name(self) -> None:
        repository = Mock()
        repository.adoption_map.return_value = {
            "candidate-1": {"display_name": "Operator name", "streams": []}
        }
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

    def test_each_synced_frigate_target_has_an_independent_status(self) -> None:
        repository = Mock()
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
                {"target": "Frigate One", "status": "applied"},
                {"target": "Frigate Two", "status": "pending"},
            ],
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
        snapshot.assert_called_once_with("stream_synthetic")

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


class TargetedRecoveryScanTests(unittest.TestCase):
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
            ):
                queued = app_module._queue_targeted_recovery_scan(repository)
                payload = json.loads(request.read_text(encoding="utf-8"))

        self.assertTrue(queued)
        self.assertEqual(payload["mode"], "targeted")
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
