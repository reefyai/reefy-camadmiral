import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camadmiral.config import (
    CamAdmiralSettings,
    FrigateIntegrationSettings,
    FrigateTargetSettings,
    IntegrationSettings,
)
from camadmiral.frigate import (
    FrigateTarget,
    desired_camera,
    frigate_camera_key,
    load_frigate_targets,
    media_host_from_inventory,
    reconcile_frigate,
)
from camadmiral.media import ProbeResult
from camadmiral.storage import CameraRepository


class FakeFrigateClient:
    def __init__(self, target: FrigateTarget):
        self.target = target
        self.current_config = {"cameras": {}}
        self.current_raw_paths = {"cameras": {}, "go2rtc": {"streams": {}}}
        self.current_runtime = {}
        self.current_stats = {}
        self.capability_checks = 0
        self.config_writes = []
        self.runtime_writes = []
        self.runtime_enabled = {}
        self.drop_add_events = 0

    def capabilities(self) -> None:
        self.capability_checks += 1

    def config(self):
        return self.current_config

    def raw_paths(self):
        return self.current_raw_paths

    def runtime_streams(self):
        return self.current_runtime

    def stats(self):
        return self.current_stats

    def set_config(self, config_data, *, update_topic=None):
        self.config_writes.append((config_data, update_topic))
        for key, update in config_data.get("cameras", {}).items():
            camera = self.current_config["cameras"].setdefault(key, {})
            self._merge(camera, update)
            if "ffmpeg" in update:
                self.current_raw_paths["cameras"][key] = {
                    "ffmpeg": {
                        "inputs": [
                            {"path": item["path"], "roles": item["roles"]}
                            for item in camera["ffmpeg"]["inputs"]
                        ]
                    }
                }
            if update_topic == f"config/cameras/{key}/add":
                if self.drop_add_events:
                    self.drop_add_events -= 1
                else:
                    self.runtime_enabled[key] = camera.get("enabled", True)
                    self.current_stats[key] = {"camera_fps": 5.0}
            elif update_topic == f"config/cameras/{key}/enabled":
                self.runtime_enabled[key] = bool(update["enabled"])
        self.current_raw_paths["go2rtc"]["streams"].update(
            config_data.get("go2rtc", {}).get("streams", {})
        )

    @classmethod
    def _merge(cls, current, update):
        for key, value in update.items():
            if isinstance(value, dict) and isinstance(current.get(key), dict):
                cls._merge(current[key], value)
            else:
                current[key] = value

    def set_runtime_stream(self, stream_name, source):
        self.runtime_writes.append((stream_name, source))
        self.current_runtime[stream_name] = {"producers": []}


class FrigateTargetTests(unittest.TestCase):
    def test_only_targets_with_camera_sync_enabled_are_loaded(self) -> None:
        configured = CamAdmiralSettings(
            integrations=IntegrationSettings(
                FrigateIntegrationSettings(
                    targets=(
                        FrigateTargetSettings(
                            "frigate-primary",
                            "Primary Frigate",
                            "http://127.0.0.1:20001",
                            True,
                        ),
                        FrigateTargetSettings(
                            "frigate-secondary",
                            "Secondary Frigate",
                            "http://127.0.0.1:20007",
                            False,
                        ),
                    ),
                    default_target="frigate-primary",
                )
            )
        )
        with patch("camadmiral.frigate.settings", return_value=configured):
            targets = load_frigate_targets()

        self.assertEqual([target.target_id for target in targets], ["frigate-primary"])
        self.assertEqual(targets[0].api_url, "http://127.0.0.1:20001")

    def test_media_host_comes_from_private_scanner_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.json"
            path.write_text(
                json.dumps({"network": {"address": "192.168.50.12"}}),
                encoding="utf-8",
            )
            self.assertEqual(media_host_from_inventory(path), "192.168.50.12")


class FrigateReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = CameraRepository(
            Path(self.temporary.name) / "camadmiral.db",
            b"k" * 32,
        )
        self.repository.migrate()
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-1", "display_name": "Entrance"},
            "operator",
            "upstream-secret",
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
                    "token": "sub",
                    "name": "Sub",
                    "uri": "rtsp://192.0.2.20/sub",
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
                by_token["main"]["stream_uuid"]: ProbeResult(
                    "ready", 20, "h264", "aac", 1920, 1080, 20
                ),
                by_token["sub"]["stream_uuid"]: ProbeResult(
                    "ready", 20, "h264", None, 640, 360, 10
                ),
            }
        )
        self.camera_uuid = adoption["camera_uuid"]
        self.target = FrigateTarget(
            "frigate-primary",
            "Primary Frigate",
            "http://127.0.0.1:20001",
        )
        self.client = FakeFrigateClient(self.target)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_desired_camera_uses_full_stable_id_and_one_shared_password(self) -> None:
        camera = self.repository.consumer_inventory()[0]
        desired = desired_camera(camera, "shared-media-secret", "192.168.50.12")

        self.assertEqual(desired["key"], frigate_camera_key(self.camera_uuid))
        self.assertTrue(desired["key"].startswith("camadmiral_"))
        self.assertEqual(len(desired["streams"]), 2)
        for sources in desired["streams"].values():
            self.assertIn("camadmiral:shared-media-secret@192.168.50.12:18554", sources[0])
            self.assertNotIn("upstream-secret", sources[0])
        self.assertEqual(desired["camera_config"]["detect"], {"width": 640, "height": 360, "fps": 10})

    def test_reconcile_persists_and_hot_adds_then_becomes_idempotent(self) -> None:
        factory = lambda _target: self.client

        first = reconcile_frigate(self.repository, self.target, client_factory=factory)
        writes_after_first = (len(self.client.config_writes), len(self.client.runtime_writes))
        second = reconcile_frigate(self.repository, self.target, client_factory=factory)

        self.assertEqual(first, {"applied": 1, "pending": 0})
        self.assertEqual(second, {"applied": 1, "pending": 0})
        self.assertEqual(writes_after_first, (2, 2))
        self.assertEqual(
            (len(self.client.config_writes), len(self.client.runtime_writes)),
            writes_after_first,
        )
        binding = self.repository.frigate_binding(self.target.target_id, self.camera_uuid)
        self.assertEqual(binding["status"], "applied")
        self.assertEqual(binding["applied_hash"], binding["desired_hash"])

    def test_missed_hot_add_waits_without_starting_duplicate_workers(self) -> None:
        self.client.drop_add_events = 1
        factory = lambda _target: self.client

        first = reconcile_frigate(self.repository, self.target, client_factory=factory)
        binding = self.repository.frigate_binding(self.target.target_id, self.camera_uuid)
        writes_after_first = (len(self.client.config_writes), len(self.client.runtime_writes))
        second = reconcile_frigate(self.repository, self.target, client_factory=factory)

        self.assertEqual(first, {"applied": 0, "pending": 1})
        self.assertEqual(binding["status"], "error")
        self.assertEqual(binding["last_error_code"], "camera_start_pending")
        self.assertEqual(second, {"applied": 0, "pending": 1})
        key = frigate_camera_key(self.camera_uuid)
        add_topics = [topic for _data, topic in self.client.config_writes if topic and topic.endswith("/add")]
        self.assertEqual(add_topics, [f"config/cameras/{key}/add"])
        self.assertEqual(
            (len(self.client.config_writes), len(self.client.runtime_writes)),
            writes_after_first,
        )

        self.client.current_stats[key] = {"camera_fps": 5.0}
        third = reconcile_frigate(self.repository, self.target, client_factory=factory)
        self.assertEqual(third, {"applied": 1, "pending": 0})

    def test_unknown_existing_key_is_not_claimed_or_modified(self) -> None:
        key = frigate_camera_key(self.camera_uuid)
        self.client.current_config["cameras"][key] = {"friendly_name": "Unrelated"}

        result = reconcile_frigate(
            self.repository,
            self.target,
            client_factory=lambda _target: self.client,
        )

        self.assertEqual(result, {"applied": 0, "pending": 1})
        self.assertEqual(self.client.config_writes, [])
        self.assertIsNone(self.repository.frigate_binding(self.target.target_id, self.camera_uuid))

    def test_reconcile_preserves_frigate_owned_camera_settings(self) -> None:
        reconcile_frigate(
            self.repository,
            self.target,
            client_factory=lambda _target: self.client,
        )
        key = frigate_camera_key(self.camera_uuid)
        self.client.current_config["cameras"][key].update(
            {
                "enabled": False,
                "zones": {"walkway": {"coordinates": "0,0,1,1"}},
                "record": {"retain": {"days": 14}},
            }
        )
        with self.repository.connect() as connection:
            connection.execute(
                "UPDATE cameras SET display_name='Renamed entrance' WHERE camera_uuid=?",
                (self.camera_uuid,),
            )
            connection.commit()

        result = reconcile_frigate(
            self.repository,
            self.target,
            client_factory=lambda _target: self.client,
        )

        camera = self.client.current_config["cameras"][key]
        self.assertEqual(result, {"applied": 1, "pending": 0})
        self.assertEqual(camera["friendly_name"], "Renamed entrance")
        self.assertFalse(camera["enabled"])
        self.assertEqual(camera["zones"], {"walkway": {"coordinates": "0,0,1,1"}})
        self.assertEqual(camera["record"], {"retain": {"days": 14}})

    def test_camadmiral_disable_and_enable_are_applied_without_losing_settings(self) -> None:
        reconcile_frigate(
            self.repository,
            self.target,
            client_factory=lambda _target: self.client,
        )
        key = frigate_camera_key(self.camera_uuid)
        self.client.current_config["cameras"][key]["zones"] = {
            "walkway": {"coordinates": "0,0,1,1"}
        }
        self.repository.set_camera_enabled(self.camera_uuid, False)

        disabled = reconcile_frigate(
            self.repository,
            self.target,
            client_factory=lambda _target: self.client,
        )

        self.assertEqual(disabled, {"applied": 1, "pending": 0})
        self.assertFalse(self.client.current_config["cameras"][key]["enabled"])
        self.assertFalse(self.client.runtime_enabled[key])
        binding = self.repository.frigate_binding(self.target.target_id, self.camera_uuid)
        self.assertFalse(binding["camera_enabled_applied"])

        self.repository.set_camera_enabled(self.camera_uuid, True)
        enabled = reconcile_frigate(
            self.repository,
            self.target,
            client_factory=lambda _target: self.client,
        )

        self.assertEqual(enabled, {"applied": 1, "pending": 0})
        self.assertTrue(self.client.current_config["cameras"][key]["enabled"])
        self.assertTrue(self.client.runtime_enabled[key])
        self.assertIn(
            (
                {"cameras": {key: {"enabled": True}}},
                f"config/cameras/{key}/enabled",
            ),
            self.client.config_writes,
        )
        self.assertEqual(
            self.client.current_config["cameras"][key]["zones"],
            {"walkway": {"coordinates": "0,0,1,1"}},
        )
        binding = self.repository.frigate_binding(self.target.target_id, self.camera_uuid)
        self.assertTrue(binding["camera_enabled_applied"])

    def test_existing_manual_frigate_disable_is_not_claimed_by_camadmiral(self) -> None:
        reconcile_frigate(
            self.repository,
            self.target,
            client_factory=lambda _target: self.client,
        )
        key = frigate_camera_key(self.camera_uuid)
        self.client.current_config["cameras"][key]["enabled"] = False
        self.repository.set_camera_enabled(self.camera_uuid, False)

        reconcile_frigate(
            self.repository,
            self.target,
            client_factory=lambda _target: self.client,
        )
        binding = self.repository.frigate_binding(self.target.target_id, self.camera_uuid)
        self.assertTrue(binding["camera_enabled_applied"])

        self.repository.set_camera_enabled(self.camera_uuid, True)
        reconcile_frigate(
            self.repository,
            self.target,
            client_factory=lambda _target: self.client,
        )

        self.assertFalse(self.client.current_config["cameras"][key]["enabled"])


if __name__ == "__main__":
    unittest.main()
