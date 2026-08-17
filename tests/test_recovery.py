import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camadmiral.media import ProbeResult
from camadmiral.recovery import recover_inventory_addresses
from camadmiral.storage import CameraRepository


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = CameraRepository(self.root / "camadmiral.db", b"k" * 32)
        self.repository.migrate()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _inventory(self, candidate: dict) -> Path:
        path = self.root / "inventory.json"
        path.write_text(json.dumps({"devices": [candidate]}), encoding="utf-8")
        return path

    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_streams")
    @patch("camadmiral.recovery.probe_source")
    def test_manual_source_follows_unique_mac_without_changing_stream_id(
        self,
        probe_source,
        probe_streams,
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
        probe_streams.side_effect = lambda sources, *_args: {
            source["stream_uuid"]: ProbeResult("ready", 25, "h264", None, 1280, 720, 15)
            for source in sources
        }
        inventory = self._inventory(
            {
                "candidate_uuid": "candidate-1",
                "ip": "192.168.1.99",
                "mac": "02:00:00:00:00:01",
                "status": "online",
            }
        )

        result = recover_inventory_addresses(self.repository, inventory)
        after = self.repository.adoption_for_candidate("candidate-1")

        self.assertEqual(result[0].status, "recovered")
        self.assertEqual(after["streams"][0]["stream_uuid"], before["streams"][0]["stream_uuid"])
        self.assertEqual(after["streams"][0]["stream_key"], before["streams"][0]["stream_key"])
        self.assertEqual(after["streams"][0]["uri"], "rtsp://192.168.1.99/live?channel=1")
        replace_streams.assert_called_once()

    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_source")
    def test_failed_validation_keeps_previous_source(self, probe_source, replace_streams) -> None:
        self.repository.adopt(
            {"candidate_uuid": "candidate-2", "display_name": "Camera"},
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

        result = recover_inventory_addresses(self.repository, inventory)
        after = self.repository.adoption_for_candidate("candidate-2")

        self.assertEqual(result[0].status, "unavailable")
        self.assertEqual(after["streams"][0]["uri"], "rtsp://192.168.1.20/live")
        replace_streams.assert_not_called()

    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_streams")
    @patch("camadmiral.recovery.probe_source")
    @patch("camadmiral.recovery.inspect_onvif_candidate")
    def test_onvif_recovery_requires_same_profile_tokens(
        self,
        inspect_onvif,
        probe_source,
        probe_streams,
        _replace_streams,
    ) -> None:
        before = self.repository.adopt(
            {"candidate_uuid": "candidate-onvif", "display_name": "Camera"},
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
        inspect_onvif.return_value = {
            "profiles": [{"token": "profile-1", "uri": "rtsp://192.168.1.77/media"}]
        }
        probe_source.return_value = ProbeResult("ready", 20, "h264", None, 1920, 1080, 20)
        probe_streams.side_effect = lambda sources, *_args: {
            source["stream_uuid"]: ProbeResult("ready", 25, "h264", None, 1920, 1080, 20)
            for source in sources
        }
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
