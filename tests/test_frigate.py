import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from camadmiral.frigate import (
    FrigateApiError,
    FrigateClient,
    FrigateTarget,
    desired_camera,
    full_sync_frigate,
    full_sync_preview,
    frigate_camera_key,
    load_frigate_targets,
    media_host_from_inventory,
    normalize_frigate_api_url,
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
        self.runtime_deletes = []
        self.runtime_delete_failures = {}
        self.runtime_enabled = {}
        self.restart_calls = 0
        self.retain_removed_camera_stats = False
        self.drop_add_events = 0
        self.operations = []

    def capabilities(self) -> None:
        self.capability_checks += 1

    def config(self):
        return self.current_config

    def raw_paths(self):
        return self.current_raw_paths

    def raw_config(self):
        return self.current_raw_paths

    def runtime_streams(self):
        return self.current_runtime

    def stats(self):
        return self.current_stats

    def set_config(self, config_data, *, update_topic=None):
        self.operations.append(("set_config", config_data))
        self.config_writes.append((config_data, update_topic))
        for key, update in config_data.get("cameras", {}).items():
            if update is None or update == "":
                self.current_config["cameras"].pop(key, None)
                self.current_raw_paths["cameras"].pop(key, None)
                if not self.retain_removed_camera_stats:
                    self.current_stats.pop(key, None)
                self.runtime_enabled.pop(key, None)
                continue
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
        for key, update in config_data.get("go2rtc", {}).get("streams", {}).items():
            if update is None or update == "":
                self.current_raw_paths["go2rtc"]["streams"].pop(key, None)
            else:
                self.current_raw_paths["go2rtc"]["streams"][key] = update

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

    def delete_runtime_stream(self, stream_name):
        self.operations.append(("delete_runtime_stream", stream_name))
        self.runtime_deletes.append(stream_name)
        if self.runtime_delete_failures.get(stream_name) == "before":
            raise FrigateApiError(
                "request_rejected",
                upstream_status=400,
                upstream_detail="synthetic rejection",
            )
        self.current_runtime.pop(stream_name, None)
        if self.runtime_delete_failures.get(stream_name) == "after":
            raise FrigateApiError(
                "request_rejected",
                upstream_status=400,
                upstream_detail="yaml: path not exist",
            )

    def restart(self):
        self.operations.append(("restart",))
        self.restart_calls += 1
        self.current_stats = {
            key: value
            for key, value in self.current_stats.items()
            if key in self.current_config["cameras"]
        }


class FrigateTargetTests(unittest.TestCase):
    def test_restart_uses_supported_frigate_endpoint(self) -> None:
        target = FrigateTarget(
            "frigate-primary",
            "Primary Frigate",
            "http://127.0.0.1:20001",
        )
        client = FrigateClient(target)
        with patch.object(
            client,
            "_request",
            return_value={"success": True, "message": "Restarting"},
        ) as request:
            client.restart()

        request.assert_called_once_with("POST", "/api/restart")

    def test_frigate_017_camera_stats_are_read_from_nested_cameras(self) -> None:
        target = FrigateTarget(
            "frigate-primary",
            "Primary Frigate",
            "http://127.0.0.1:20001",
        )
        client = FrigateClient(target)
        with patch.object(
            client,
            "_request",
            return_value={
                "cameras": {"camadmiral_synthetic": {"camera_fps": 5.0}},
                "service": {"uptime": 60},
            },
        ):
            stats = client.stats()

        self.assertEqual(stats, {"camadmiral_synthetic": {"camera_fps": 5.0}})

    def test_saved_frigate_config_is_parsed_from_raw_yaml(self) -> None:
        target = FrigateTarget(
            "frigate-primary",
            "Primary Frigate",
            "http://127.0.0.1:20001",
        )
        client = FrigateClient(target)
        with patch.object(
            client,
            "_request",
            return_value=(
                "cameras:\n"
                "  camadmiral_saved:\n"
                "    enabled: false\n"
                "go2rtc:\n"
                "  streams:\n"
                "    camadmiral_saved_record:\n"
                "      - rtsp://camera.invalid/main\n"
            ),
        ):
            config = client.raw_config()

        self.assertIn("camadmiral_saved", config["cameras"])
        self.assertIn("camadmiral_saved_record", config["go2rtc"]["streams"])

    def test_http_error_detail_is_bounded_and_redacts_url_credentials(self) -> None:
        target = FrigateTarget(
            "frigate-primary",
            "Primary Frigate",
            "http://127.0.0.1:20001",
        )
        client = FrigateClient(target)
        response = json.dumps(
            {
                "message": (
                    "Failed rtsp://operator:synthetic-secret@192.0.2.20/live: "
                    "yaml: path not exist"
                )
            }
        ).encode("utf-8")
        error = urllib.error.HTTPError(
            "http://127.0.0.1:20001/api/go2rtc/streams/test",
            400,
            "Bad Request",
            {},
            io.BytesIO(response),
        )

        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(FrigateApiError) as raised:
                client.runtime_streams()

        self.assertEqual(raised.exception.upstream_status, 400)
        self.assertIn("rtsp://***@192.0.2.20/live", raised.exception.upstream_detail)
        self.assertNotIn("synthetic-secret", raised.exception.upstream_detail)

    def test_only_targets_with_camera_sync_enabled_are_loaded(self) -> None:
        repository = Mock()
        repository.frigate_targets.return_value = [
            {
                "target_id": "frigate-primary",
                "name": "Primary Frigate",
                "api_url": "http://127.0.0.1:20001",
            }
        ]
        targets = load_frigate_targets(repository)

        self.assertEqual([target.target_id for target in targets], ["frigate-primary"])
        self.assertEqual(targets[0].api_url, "http://127.0.0.1:20001")
        repository.frigate_targets.assert_called_once_with(sync_only=True)

    def test_frigate_url_is_normalized_and_restricted_to_loopback(self) -> None:
        self.assertEqual(
            normalize_frigate_api_url("http://127.0.0.1:20001/"),
            "http://127.0.0.1:20001",
        )
        with self.assertRaises(FrigateApiError) as raised:
            normalize_frigate_api_url("http://192.0.2.10:5000")
        self.assertEqual(raised.exception.code, "invalid_target_url")

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

    def test_full_sync_removes_only_stale_camadmiral_resources(self) -> None:
        stale_camera = "camadmiral_stale_camera"
        stale_streams = {f"{stale_camera}_record", f"{stale_camera}_detect"}
        self.client.current_config["cameras"].update(
            {
                stale_camera: {"enabled": True},
                "operator_camera": {"enabled": True},
            }
        )
        self.client.current_raw_paths["cameras"].update(
            {
                stale_camera: {"ffmpeg": {"inputs": []}},
                "operator_camera": {"ffmpeg": {"inputs": []}},
            }
        )
        self.client.current_raw_paths["go2rtc"]["streams"].update(
            {
                **{name: ["rtsp://stale.invalid/stream"] for name in stale_streams},
                "operator_stream": ["rtsp://camera.invalid/stream"],
            }
        )
        self.client.current_runtime.update({name: {} for name in stale_streams})

        preview = full_sync_preview(
            self.repository,
            self.target,
            client_factory=lambda _target: self.client,
        )
        result = full_sync_frigate(
            self.repository,
            self.target,
            media_host="192.168.50.12",
            client_factory=lambda _target: self.client,
        )

        self.assertEqual(preview["stale_cameras"], [stale_camera])
        self.assertEqual(set(preview["stale_streams"]), stale_streams)
        self.assertEqual(result["removed_cameras"], 1)
        self.assertEqual(result["removed_streams"], 2)
        self.assertIn("operator_camera", self.client.current_config["cameras"])
        self.assertIn("operator_stream", self.client.current_raw_paths["go2rtc"]["streams"])
        self.assertNotIn(stale_camera, self.client.current_config["cameras"])
        self.assertTrue(stale_streams.isdisjoint(self.client.current_raw_paths["go2rtc"]["streams"]))
        self.assertEqual(set(self.client.runtime_deletes), stale_streams)
        self.assertIn(
            ({"cameras": {stale_camera: ""}}, f"config/cameras/{stale_camera}/remove"),
            self.client.config_writes,
        )
        self.assertIn(
            ({"go2rtc": {"streams": {name: "" for name in stale_streams}}}, None),
            self.client.config_writes,
        )
        stream_config_index = next(
            index
            for index, operation in enumerate(self.client.operations)
            if operation
            == (
                "set_config",
                {"go2rtc": {"streams": {name: "" for name in stale_streams}}},
            )
        )
        runtime_delete_indexes = [
            index
            for index, operation in enumerate(self.client.operations)
            if operation[0] == "delete_runtime_stream"
        ]
        self.assertTrue(runtime_delete_indexes)
        self.assertTrue(
            all(stream_config_index < index for index in runtime_delete_indexes)
        )
        self.assertEqual(self.client.restart_calls, 0)

    def test_full_sync_restarts_when_removed_camera_worker_remains(self) -> None:
        stale_camera = "camadmiral_stale_worker"
        stale_streams = {f"{stale_camera}_record", f"{stale_camera}_detect"}
        self.client.current_config["cameras"][stale_camera] = {"enabled": True}
        self.client.current_raw_paths["cameras"][stale_camera] = {
            "ffmpeg": {"inputs": []}
        }
        self.client.current_raw_paths["go2rtc"]["streams"].update(
            {name: ["rtsp://stale.invalid/stream"] for name in stale_streams}
        )
        self.client.current_runtime.update({name: {} for name in stale_streams})
        self.client.current_stats[stale_camera] = {"camera_fps": 0.0}
        self.client.retain_removed_camera_stats = True

        with patch("camadmiral.frigate.CAMERA_DYNAMIC_CLEANUP_GRACE_SECONDS", 0):
            result = full_sync_frigate(
                self.repository,
                self.target,
                media_host="192.168.50.12",
                client_factory=lambda _target: self.client,
            )

        self.assertEqual(result["removed_cameras"], 1)
        self.assertEqual(self.client.restart_calls, 1)
        self.assertNotIn(stale_camera, self.client.current_stats)
        self.assertIn(("restart",), self.client.operations)

    def test_full_sync_tolerates_configured_stream_missing_from_runtime(self) -> None:
        stale_camera = "camadmiral_partial_drift"
        stale_record = f"{stale_camera}_record"
        stale_detect = f"{stale_camera}_detect"
        self.client.current_raw_paths["go2rtc"]["streams"].update(
            {
                stale_record: ["rtsp://stale.invalid/main"],
                stale_detect: ["rtsp://stale.invalid/sub"],
            }
        )
        self.client.current_runtime[stale_record] = {"producers": []}

        result = full_sync_frigate(
            self.repository,
            self.target,
            media_host="192.168.50.12",
            client_factory=lambda _target: self.client,
        )

        self.assertEqual(result["removed_streams"], 2)
        self.assertEqual(self.client.runtime_deletes, [stale_record])
        self.assertNotIn(stale_record, self.client.current_raw_paths["go2rtc"]["streams"])
        self.assertNotIn(stale_detect, self.client.current_raw_paths["go2rtc"]["streams"])

    def test_full_sync_accepts_rejected_delete_when_live_stream_is_gone(self) -> None:
        stale_stream = "camadmiral_ambiguous_delete_detect"
        self.client.current_raw_paths["go2rtc"]["streams"][stale_stream] = [
            "rtsp://stale.invalid/sub"
        ]
        self.client.current_runtime[stale_stream] = {"producers": []}
        self.client.runtime_delete_failures[stale_stream] = "after"

        result = full_sync_frigate(
            self.repository,
            self.target,
            media_host="192.168.50.12",
            client_factory=lambda _target: self.client,
        )

        self.assertEqual(result["removed_streams"], 1)
        self.assertNotIn(stale_stream, self.client.current_runtime)
        self.assertNotIn(stale_stream, self.client.current_raw_paths["go2rtc"]["streams"])

    def test_full_sync_waits_for_frigate_runtime_cleanup_propagation(self) -> None:
        stale_stream = "camadmiral_delayed_cleanup_detect"
        self.client.current_raw_paths["go2rtc"]["streams"][stale_stream] = [
            "rtsp://stale.invalid/sub"
        ]
        self.client.current_runtime[stale_stream] = {"producers": []}
        runtime_streams = self.client.runtime_streams
        delayed_reads = 0

        def delayed_runtime_streams():
            nonlocal delayed_reads
            current = runtime_streams()
            if self.client.runtime_deletes and delayed_reads < 2:
                delayed_reads += 1
                return {**current, stale_stream: {"producers": []}}
            return current

        self.client.runtime_streams = delayed_runtime_streams
        with patch("camadmiral.frigate.time.sleep") as sleep:
            result = full_sync_frigate(
                self.repository,
                self.target,
                media_host="192.168.50.12",
                client_factory=lambda _target: self.client,
            )

        self.assertEqual(result["removed_streams"], 1)
        self.assertEqual(delayed_reads, 2)
        self.assertEqual(sleep.call_count, 2)

    def test_full_sync_removes_runtime_only_stale_stream(self) -> None:
        stale_stream = "camadmiral_runtime_only_detect"
        self.client.current_runtime[stale_stream] = {"producers": []}
        self.client.runtime_delete_failures[stale_stream] = "after"

        preview = full_sync_preview(
            self.repository,
            self.target,
            client_factory=lambda _target: self.client,
        )
        result = full_sync_frigate(
            self.repository,
            self.target,
            media_host="192.168.50.12",
            client_factory=lambda _target: self.client,
        )

        self.assertEqual(preview["stale_streams"], [stale_stream])
        self.assertEqual(result["removed_streams"], 1)
        self.assertNotIn(stale_stream, self.client.current_runtime)
        self.assertFalse(
            any(
                config.get("go2rtc", {}).get("streams", {}).get(stale_stream) == ""
                for config, _topic in self.client.config_writes
            )
        )

    def test_full_sync_rejects_failed_delete_when_live_stream_remains(self) -> None:
        stale_stream = "camadmiral_rejected_delete_detect"
        self.client.current_raw_paths["go2rtc"]["streams"][stale_stream] = [
            "rtsp://stale.invalid/sub"
        ]
        self.client.current_runtime[stale_stream] = {"producers": []}
        self.client.runtime_delete_failures[stale_stream] = "before"

        with (
            patch("camadmiral.frigate.RUNTIME_CLEANUP_TIMEOUT_SECONDS", 0),
            self.assertRaises(FrigateApiError) as raised,
        ):
            full_sync_frigate(
                self.repository,
                self.target,
                media_host="192.168.50.12",
                client_factory=lambda _target: self.client,
            )

        self.assertEqual(raised.exception.code, "request_rejected")
        self.assertEqual(raised.exception.stage, "remove_runtime_stream")
        self.assertEqual(raised.exception.resource, stale_stream)
        self.assertIn(stale_stream, self.client.current_runtime)
        self.assertNotIn(stale_stream, self.client.current_raw_paths["go2rtc"]["streams"])

        self.client.runtime_delete_failures.pop(stale_stream)
        retry = full_sync_frigate(
            self.repository,
            self.target,
            media_host="192.168.50.12",
            client_factory=lambda _target: self.client,
        )
        self.assertEqual(retry["removed_streams"], 1)
        self.assertNotIn(stale_stream, self.client.current_runtime)

    def test_full_sync_preview_uses_saved_config_instead_of_live_view(self) -> None:
        stale_camera = "camadmiral_saved_only"
        stale_stream = f"{stale_camera}_record"
        self.client.raw_config = lambda: {
            "cameras": {stale_camera: {"enabled": False}},
            "go2rtc": {"streams": {stale_stream: ["rtsp://stale.invalid/main"]}},
        }
        self.client.raw_paths = lambda: {"cameras": {}, "go2rtc": {"streams": {}}}

        preview = full_sync_preview(
            self.repository,
            self.target,
            client_factory=lambda _target: self.client,
        )

        self.assertEqual(preview["stale_cameras"], [stale_camera])
        self.assertEqual(preview["stale_streams"], [stale_stream])

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

    def test_idle_role_stream_remains_valid_frigate_configuration(self) -> None:
        camera = self.repository.consumer_inventory()[0]
        record = next(stream for stream in camera["streams"] if "record" in stream["roles"])
        record["health_status"] = "unknown"
        record["probe_status"] = "idle"

        desired = desired_camera(camera, "shared-media-secret", "192.168.50.12")

        self.assertIsNotNone(desired)
        self.assertEqual(len(desired["camera_config"]["ffmpeg"]["inputs"]), 2)

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
