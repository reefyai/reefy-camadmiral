import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from camadmiral.media import ProbeResult
from camadmiral.recovery import (
    ATTEMPT_COUNTS,
    ATTEMPTED_AT,
    _validated_updates,
    recover_inventory_addresses,
)
from camadmiral.storage import CameraRepository


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        ATTEMPTED_AT.clear()
        ATTEMPT_COUNTS.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = CameraRepository(self.root / "camadmiral.db", b"k" * 32)
        self.repository.migrate()

    def tearDown(self) -> None:
        ATTEMPTED_AT.clear()
        ATTEMPT_COUNTS.clear()
        self.temporary.cleanup()

    def _inventory(self, candidate: dict) -> Path:
        path = self.root / "inventory.json"
        path.write_text(json.dumps({"devices": [candidate]}), encoding="utf-8")
        return path

    @patch("camadmiral.recovery.probe_source")
    def test_replacement_sources_are_validated_in_parallel(self, probe_source) -> None:
        barrier = threading.Barrier(3)

        def validate(*_args):
            barrier.wait(timeout=1)
            return ProbeResult("ready", 20)

        probe_source.side_effect = validate
        streams = [
            {
                "profile_token": f"profile-{index}",
                "source_kind": "manual_rtsp",
                "uri": f"rtsp://192.0.2.10/stream-{index}",
            }
            for index in range(3)
        ]

        updates, status = _validated_updates(
            {"ip": "192.0.2.20"},
            {"streams": streams},
            "operator",
            "synthetic-secret",
        )

        self.assertEqual(status, "ready")
        self.assertEqual(set(updates), {"profile-0", "profile-1", "profile-2"})
        self.assertTrue(
            all(uri.startswith("rtsp://192.0.2.20/") for uri in updates.values())
        )

    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_source")
    def test_manual_source_follows_unique_mac_without_changing_stream_id(
        self,
        probe_source,
        replace_streams,
    ) -> None:
        before = self.repository.adopt(
            {"candidate_uuid": "candidate-1", "display_name": "Camera"},
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "manual-stream",
                    "name": "Stream",
                    "uri": "rtsp://192.168.1.20/live?channel=1",
                    "width": 1280,
                    "height": 720,
                    "encoding": "H264",
                    "fps": 15,
                    "bitrate_kbps": 0,
                    "source_kind": "manual_rtsp",
                }
            ],
            {"record": "manual-stream", "detect": "manual-stream"},
        )
        probe_source.return_value = ProbeResult("ready", 20, "h264", None, 1280, 720, 15)
        inventory = self._inventory(
            {
                "candidate_uuid": "candidate-1",
                "ip": "192.168.1.99",
                "mac": "02:00:00:00:00:01",
                "status": "online",
            }
        )
        stale_attempt = (str(before["camera_uuid"]), "192.168.1.88")
        ATTEMPTED_AT[stale_attempt] = 9.0
        ATTEMPT_COUNTS[stale_attempt] = 5

        with patch("camadmiral.recovery.time.monotonic", return_value=10.0):
            result = recover_inventory_addresses(self.repository, inventory)
        after = self.repository.adoption_for_candidate("candidate-1")

        self.assertEqual(result[0].status, "recovered")
        self.assertEqual(after["streams"][0]["stream_uuid"], before["streams"][0]["stream_uuid"])
        self.assertEqual(after["streams"][0]["stream_key"], before["streams"][0]["stream_key"])
        self.assertEqual(after["streams"][0]["uri"], "rtsp://192.168.1.99/live?channel=1")
        self.assertFalse(
            any(key[0] == str(before["camera_uuid"]) for key in ATTEMPTED_AT)
        )
        replace_streams.assert_called_once()

    def test_current_inventory_address_clears_stale_retry_history(self) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-current", "display_name": "Camera"},
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "manual-stream",
                    "name": "Stream",
                    "uri": "rtsp://192.168.2.20/live",
                    "width": 1280,
                    "height": 720,
                    "encoding": "H264",
                    "fps": 15,
                    "bitrate_kbps": 0,
                    "source_kind": "manual_rtsp",
                }
            ],
            {"record": "manual-stream", "detect": "manual-stream"},
        )
        camera_uuid = str(adoption["camera_uuid"])
        stale_attempt = (camera_uuid, "192.168.2.99")
        ATTEMPTED_AT[stale_attempt] = 100.0
        ATTEMPT_COUNTS[stale_attempt] = 5
        inventory = self._inventory(
            {
                "candidate_uuid": "candidate-current",
                "ip": "192.168.2.20",
                "mac": "02:00:00:00:02:20",
                "status": "online",
            }
        )

        result = recover_inventory_addresses(self.repository, inventory)

        self.assertEqual(result, [])
        self.assertFalse(any(key[0] == camera_uuid for key in ATTEMPTED_AT))
        self.assertFalse(any(key[0] == camera_uuid for key in ATTEMPT_COUNTS))

    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_source")
    def test_conflicted_inventory_cannot_retarget_camera(
        self,
        probe_source,
        replace_streams,
    ) -> None:
        self.repository.adopt(
            {
                "candidate_uuid": "candidate-conflict",
                "display_name": "Synthetic camera",
                "ip": "172.21.10.20",
                "mac": "02:00:00:00:00:20",
            },
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "manual-stream",
                    "name": "Stream",
                    "uri": "rtsp://172.21.10.20/live",
                    "width": 640,
                    "height": 360,
                    "encoding": "H264",
                    "fps": 10,
                    "bitrate_kbps": 0,
                    "source_kind": "manual_rtsp",
                }
            ],
            {"record": "manual-stream", "detect": "manual-stream"},
        )
        inventory = self._inventory(
            {
                "candidate_uuid": "candidate-conflict",
                "ip": "172.21.10.99",
                "mac": "02:00:00:00:00:99",
                "status": "online",
                "identity_conflict": True,
            }
        )

        result = recover_inventory_addresses(self.repository, inventory)
        after = self.repository.adoption_for_candidate("candidate-conflict")

        self.assertEqual(result, [])
        self.assertEqual(after["streams"][0]["uri"], "rtsp://172.21.10.20/live")
        probe_source.assert_not_called()
        replace_streams.assert_not_called()

    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_source")
    def test_failed_validation_keeps_previous_source(self, probe_source, replace_streams) -> None:
        self.repository.adopt(
            {
                "candidate_uuid": "candidate-2",
                "display_name": "Camera",
                "ip": "192.168.1.20",
                "mac": "02:00:00:00:00:01",
            },
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "manual-stream",
                    "name": "Stream",
                    "uri": "rtsp://192.168.1.20/live",
                    "width": 640,
                    "height": 360,
                    "encoding": "H264",
                    "fps": 10,
                    "bitrate_kbps": 0,
                    "source_kind": "manual_rtsp",
                }
            ],
            {"record": "manual-stream", "detect": "manual-stream"},
        )
        probe_source.return_value = ProbeResult("unavailable", 50)
        inventory = self._inventory(
            {
                "candidate_uuid": "candidate-2",
                "ip": "192.168.1.99",
                "mac": "02:00:00:00:00:02",
                "status": "online",
            }
        )

        with patch("camadmiral.recovery.time.monotonic", return_value=100.0):
            result = recover_inventory_addresses(self.repository, inventory)
        with patch("camadmiral.recovery.time.monotonic", return_value=109.0):
            deferred = recover_inventory_addresses(self.repository, inventory)
        with patch("camadmiral.recovery.time.monotonic", return_value=110.0):
            retried = recover_inventory_addresses(self.repository, inventory)
        after = self.repository.adoption_for_candidate("candidate-2")

        self.assertEqual(result[0].status, "unavailable")
        self.assertEqual(deferred, [])
        self.assertEqual(retried[0].status, "unavailable")
        self.assertEqual(probe_source.call_count, 2)
        self.assertEqual(after["streams"][0]["uri"], "rtsp://192.168.1.20/live")
        replace_streams.assert_not_called()

    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_source")
    def test_failed_validation_retries_and_applies_ready_source_after_ten_seconds(
        self,
        probe_source,
        replace_streams,
    ) -> None:
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-fast-retry", "display_name": "Camera"},
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "manual-stream",
                    "name": "Stream",
                    "uri": "rtsp://192.168.3.20/live",
                    "width": 640,
                    "height": 360,
                    "encoding": "H264",
                    "fps": 10,
                    "bitrate_kbps": 0,
                    "source_kind": "manual_rtsp",
                }
            ],
            {"record": "manual-stream", "detect": "manual-stream"},
        )
        probe_source.side_effect = [
            ProbeResult("unavailable", 50),
            ProbeResult("ready", 20, "h264", None, 640, 360, 10),
        ]
        inventory = self._inventory(
            {
                "candidate_uuid": "candidate-fast-retry",
                "ip": "192.168.3.99",
                "mac": "02:00:00:00:03:99",
                "status": "online",
            }
        )

        with patch("camadmiral.recovery.time.monotonic", return_value=100.0):
            first = recover_inventory_addresses(self.repository, inventory)
        with patch("camadmiral.recovery.time.monotonic", return_value=109.0):
            deferred = recover_inventory_addresses(self.repository, inventory)
        with patch("camadmiral.recovery.time.monotonic", return_value=110.0):
            recovered = recover_inventory_addresses(self.repository, inventory)

        after = self.repository.adoption_for_candidate("candidate-fast-retry")
        self.assertEqual(first[0].status, "unavailable")
        self.assertEqual(deferred, [])
        self.assertEqual(recovered[0].status, "recovered")
        self.assertEqual(after["camera_uuid"], adoption["camera_uuid"])
        self.assertEqual(after["streams"][0]["uri"], "rtsp://192.168.3.99/live")
        self.assertEqual(probe_source.call_count, 2)
        replace_streams.assert_called_once()
        self.assertFalse(any(key[0] == adoption["camera_uuid"] for key in ATTEMPTED_AT))

    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_source")
    def test_failed_live_patch_reuses_live_update_for_rollback(
        self,
        probe_source,
        replace_streams,
    ) -> None:
        self.repository.adopt(
            {"candidate_uuid": "candidate-rollback", "display_name": "Camera"},
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "manual-stream",
                    "name": "Stream",
                    "uri": "rtsp://192.168.1.20/live",
                    "width": 640,
                    "height": 360,
                    "encoding": "H264",
                    "fps": 10,
                    "bitrate_kbps": 0,
                    "source_kind": "manual_rtsp",
                }
            ],
            {"record": "manual-stream", "detect": "manual-stream"},
        )
        probe_source.return_value = ProbeResult("ready", 20, "h264", None, 640, 360, 10)
        replace_streams.side_effect = [RuntimeError("synthetic PATCH failure"), None]
        inventory = self._inventory(
            {
                "candidate_uuid": "candidate-rollback",
                "ip": "192.168.1.99",
                "mac": "02:00:00:00:00:04",
                "status": "online",
            }
        )

        result = recover_inventory_addresses(self.repository, inventory)
        after = self.repository.adoption_for_candidate("candidate-rollback")

        self.assertEqual(result[0].status, "runtime_failed")
        self.assertEqual(after["streams"][0]["uri"], "rtsp://192.168.1.20/live")
        self.assertEqual(replace_streams.call_count, 2)
        promoted = replace_streams.call_args_list[0].args[0]
        rolled_back = replace_streams.call_args_list[1].args[0]
        self.assertEqual(promoted[0]["uri"], "rtsp://192.168.1.99/live")
        self.assertEqual(rolled_back[0]["uri"], "rtsp://192.168.1.20/live")

    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_source")
    @patch("camadmiral.recovery.inspect_onvif_candidate")
    def test_auth_failed_onvif_recovery_revalidates_same_profile_tokens(
        self,
        inspect_onvif,
        probe_source,
        _replace_streams,
    ) -> None:
        before = self.repository.adopt(
            {
                "candidate_uuid": "candidate-onvif",
                "display_name": "Camera",
                "ip": "192.168.1.20",
                "mac": "02:00:00:00:00:02",
                "onvif": {"endpoint_reference": "urn:uuid:synthetic-camera"},
            },
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "profile-1",
                    "name": "Camera profile",
                    "uri": "rtsp://192.168.1.20/media",
                    "width": 1920,
                    "height": 1080,
                    "encoding": "H264",
                    "fps": 20,
                    "bitrate_kbps": 0,
                    "source_kind": "onvif",
                }
            ],
            {"record": "profile-1", "detect": "profile-1"},
        )
        self.repository.record_camera_auth_failure(
            before["camera_uuid"],
            ProbeResult("auth_failed", 10),
        )
        failed = self.repository.adoption_for_candidate("candidate-onvif")
        self.assertEqual(failed["streams"][0]["health_status"], "auth_failed")
        inspect_onvif.return_value = {
            "profiles": [{"token": "profile-1", "uri": "rtsp://192.168.1.77/media"}]
        }
        probe_source.return_value = ProbeResult("ready", 20, "h264", None, 1920, 1080, 20)
        inventory = self._inventory(
            {
                "candidate_uuid": "candidate-onvif",
                "ip": "192.168.1.77",
                "mac": "02:00:00:00:00:03",
                "status": "online",
                "onvif": {"endpoint_reference": "urn:uuid:synthetic-camera"},
            }
        )

        result = recover_inventory_addresses(self.repository, inventory)
        after = self.repository.adoption_for_candidate("candidate-onvif")

        self.assertEqual(result[0].status, "recovered")
        self.assertEqual(after["streams"][0]["stream_uuid"], before["streams"][0]["stream_uuid"])
        self.assertEqual(after["streams"][0]["uri"], "rtsp://192.168.1.77/media")


if __name__ == "__main__":
    unittest.main()
