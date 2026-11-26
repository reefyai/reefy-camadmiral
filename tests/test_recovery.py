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
        self.probe_streams_patcher = patch("camadmiral.recovery.probe_streams")
        self.probe_streams = self.probe_streams_patcher.start()
        self.probe_streams.side_effect = lambda sources, *_args: {
            source["stream_uuid"]: ProbeResult("ready", 20) for source in sources
        }

    def tearDown(self) -> None:
        ATTEMPTED_AT.clear()
        ATTEMPT_COUNTS.clear()
        self.probe_streams_patcher.stop()
        self.temporary.cleanup()

    def _inventory(self, candidate: dict | list[dict]) -> Path:
        path = self.root / "inventory.json"
        devices = candidate if isinstance(candidate, list) else [candidate]
        path.write_text(json.dumps({"devices": devices}), encoding="utf-8")
        return path

    def _enable_notifications(self) -> None:
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

    def _recovery_fixture(self, *, profiles=1):
        adoption = self.repository.adopt(
            {"candidate_uuid": "candidate-verification", "display_name": "Synthetic entrance"},
            "operator", "synthetic-secret",
            [{"token": f"profile-{i}", "name": f"Profile {i}",
              "uri": f"rtsp://192.0.2.10/stream-{i}?password=synthetic-query-secret",
              "width": 640, "height": 360, "encoding": "H264", "fps": 10,
              "bitrate_kbps": 0, "source_kind": "manual_rtsp"} for i in range(profiles)],
            {"record": "profile-0", "detect": "profile-0"})
        inventory = self._inventory({"candidate_uuid": "candidate-verification",
            "ip": "192.0.2.20", "mac": "02:00:00:00:20:01", "status": "online"})
        return adoption, inventory

    @patch("camadmiral.recovery.time.sleep")
    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_source", return_value=ProbeResult("ready", 10))
    def test_transient_relay_verification_retries_without_another_restart(self, _source, replace, sleep):
        before, inventory = self._recovery_fixture(profiles=3)
        calls = []
        failing = before["streams"][-1]["stream_uuid"]
        def probe(sources, *_args):
            calls.append({s["stream_uuid"] for s in sources})
            return {s["stream_uuid"]: ProbeResult(
                "timeout" if len(calls) == 1 and s["stream_uuid"] == failing else "ready", 10)
                for s in sources}
        self.probe_streams.side_effect = probe
        result = recover_inventory_addresses(self.repository, inventory)
        self.assertEqual(result[0].status, "recovered")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], {failing})
        replace.assert_called_once()
        self.assertEqual(sleep.call_count, 1)
        after = self.repository.adoption_for_candidate("candidate-verification")
        self.assertEqual([s["stream_uuid"] for s in before["streams"]],
                         [s["stream_uuid"] for s in after["streams"]])
        self.assertTrue(all("192.0.2.20" in s["uri"] for s in after["streams"]))

    @patch("camadmiral.recovery.time.sleep")
    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_source", return_value=ProbeResult("ready", 10))
    def test_unused_enabled_profile_remains_required_and_retries_are_bounded(self, _source, replace, sleep):
        before, inventory = self._recovery_fixture(profiles=3)
        failing = before["streams"][-1]["stream_uuid"]
        def probe(sources, *_args):
            return {s["stream_uuid"]: ProbeResult(
                "timeout" if s["stream_uuid"] == failing else "ready", 10) for s in sources}
        self.probe_streams.side_effect = probe
        result = recover_inventory_addresses(self.repository, inventory)
        self.assertEqual(result[0].status, "runtime_failed")
        self.assertEqual(self.probe_streams.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(replace.call_count, 2)
        after = self.repository.adoption_for_candidate("candidate-verification")
        self.assertEqual([s["uri"] for s in before["streams"]], [s["uri"] for s in after["streams"]])
        self.assertEqual(self.repository.incidents(status="open")["open_count"], 1)

    @patch("camadmiral.recovery.time.sleep")
    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_source", return_value=ProbeResult("ready", 10))
    def test_relay_auth_failure_is_not_retried(self, _source, replace, sleep):
        _before, inventory = self._recovery_fixture()
        self.probe_streams.side_effect = lambda sources, *_args: {
            s["stream_uuid"]: ProbeResult("auth_failed", 10) for s in sources}
        self.assertEqual(recover_inventory_addresses(self.repository, inventory)[0].status, "runtime_failed")
        self.probe_streams.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(replace.call_count, 2)

    def _pending_address_recovery(self):
        self._enable_notifications()
        before, _inventory = self._recovery_fixture(profiles=3)
        for _ in range(3):
            self.repository.record_probe_results({s["stream_uuid"]: ProbeResult("unavailable", 10)
                                                 for s in before["streams"]})
        incident = self.repository.open_camera_address_incident(
            before["camera_uuid"], "192.0.2.10", "192.0.2.20", "unique-mac")
        ready = {s["stream_uuid"]: ProbeResult("ready", 10) for s in before["streams"]}
        return before, incident, ready

    def test_recovery_notice_is_atomic_for_a_concurrent_outbox_reader_and_idempotent(self):
        from concurrent.futures import ThreadPoolExecutor
        _before, incident, ready = self._pending_address_recovery()
        original = self.repository._record_probe_results
        intermediate = []
        def record(connection, results, timestamp):
            original(connection, results, timestamp)
            with ThreadPoolExecutor(max_workers=1) as sender:
                intermediate.extend(sender.submit(self.repository.due_notifications).result(timeout=2))
        with patch.object(self.repository, '_record_probe_results', side_effect=record):
            self.repository.complete_address_recovery([incident], ready)
        self.assertFalse(any(n['event_type']=='incident_resolved' for n in intermediate))
        self.repository.complete_address_recovery([incident], ready)
        resolved = [n for n in self.repository.due_notifications() if n['event_type']=='incident_resolved']
        self.assertEqual([n['payload']['kind'] for n in resolved], ['camera_address_changed'])
        self.assertEqual(self.repository.incidents(status='open')['open_count'], 0)

    def test_failed_completion_rolls_back_health_and_notifications(self):
        before, incident, ready = self._pending_address_recovery()
        resolve = self.repository._resolve_incident
        def fail_address(connection, incident_uuid, timestamp, reason):
            if incident_uuid == incident:
                raise RuntimeError('synthetic commit failure')
            return resolve(connection, incident_uuid, timestamp, reason)
        with patch.object(self.repository, '_resolve_incident', side_effect=fail_address):
            with self.assertRaises(RuntimeError):
                self.repository.complete_address_recovery([incident], ready)
        self.assertEqual(self.repository.incidents(status='open')['open_count'], 2)
        self.assertFalse(any(n['event_type']=='incident_resolved' for n in self.repository.due_notifications()))
        self.assertEqual({s['probe_status'] for s in self.repository.adoption_for_candidate(
            'candidate-verification')['streams']}, {'unavailable'})
        self.assertEqual(before['camera_uuid'], self.repository.adoption_for_candidate(
            'candidate-verification')['camera_uuid'])

    def test_incomplete_completion_never_drops_an_enabled_unused_profile(self):
        _before, incident, ready = self._pending_address_recovery()
        ready.pop(next(iter(ready)))
        with self.assertRaises(ValueError):self.repository.complete_address_recovery([incident], ready)
        self.assertEqual(self.repository.incidents(status='open')['open_count'], 2)
        self.assertFalse(any(n['event_type']=='incident_resolved' for n in self.repository.due_notifications()))

    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_source")
    def test_upstream_diagnostics_cover_every_profile_without_source_secrets(self, probe, replace):
        before, inventory = self._recovery_fixture(profiles=3)
        probe.side_effect = [ProbeResult("timeout", 8000), ProbeResult("auth_failed", 12),
                             RuntimeError("rtsp://operator:synthetic-secret@192.0.2.20/private")]
        with patch("builtins.print") as log:
            self.assertEqual(recover_inventory_addresses(self.repository, inventory)[0].status in
                             {"timeout", "auth_failed", "error"}, True)
        lines = [str(call.args[0]) for call in log.call_args_list
                 if str(call.args[0]).startswith("media-recovery: ")]
        events = [json.loads(line.split(": ", 1)[1]) for line in lines]
        profiles = [e for e in events if e.get("stage") == "upstream" and e.get("event") == "probe"]
        self.assertEqual({e["stream_uuid"] for e in profiles}, {s["stream_uuid"] for s in before["streams"]})
        self.assertEqual({e["status"] for e in profiles}, {"timeout", "auth_failed", "error"})
        text = "\n".join(lines)
        for secret in ("synthetic-secret", "synthetic-query-secret", "rtsp://", "profile-0", "operator"):
            self.assertNotIn(secret, text)
        replace.assert_not_called()

    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_source", return_value=ProbeResult("ready", 10))
    def test_address_recovery_emits_one_resolved_notice_preserving_both_incidents(self, _source, _replace):
        self._enable_notifications()
        before, inventory = self._recovery_fixture()
        stream = before["streams"][0]["stream_uuid"]
        for _ in range(3):self.repository.record_probe_results({stream: ProbeResult("unavailable", 10)})
        self.assertEqual(recover_inventory_addresses(self.repository, inventory)[0].status, "recovered")
        events = [x for x in self.repository.due_notifications() if x["event_type"] == "incident_resolved"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["kind"], "camera_address_changed")
        self.assertEqual({x["kind"] for x in self.repository.incidents(status="resolved")["incidents"]},
                         {"media_offline", "camera_address_changed"})

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
        stream_uuid = str(before["streams"][0]["stream_uuid"])
        for _ in range(3):
            self.repository.record_probe_results(
                {stream_uuid: ProbeResult("unavailable", 20)}
            )
        self.assertEqual(self.repository.incidents(status="open")["open_count"], 1)
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
        self.assertEqual(after["streams"][0]["probed_width"], 1280)
        self.assertEqual(after["streams"][0]["probed_height"], 720)
        self.assertEqual(after["streams"][0]["video_codec"], "h264")
        self.assertFalse(
            any(key[0] == str(before["camera_uuid"]) for key in ATTEMPTED_AT)
        )
        replace_streams.assert_called_once()
        self.assertEqual(self.repository.incidents(status="open")["open_count"], 0)
        self.assertEqual(
            {incident["kind"] for incident in self.repository.incidents(status="resolved")["incidents"]},
            {"media_offline", "camera_address_changed"},
        )

    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_source")
    def test_two_camera_moves_are_applied_with_one_relay_restart(
        self,
        probe_source,
        replace_streams,
    ) -> None:
        self._enable_notifications()
        for index, address in enumerate(("192.0.2.20", "192.0.2.30"), start=1):
            self.repository.adopt(
                {
                    "candidate_uuid": f"candidate-batch-{index}",
                    "display_name": f"Camera {index}",
                    "ip": address,
                    "mac": f"02:00:00:00:10:{index:02x}",
                },
                "operator",
                "synthetic-secret",
                [
                    {
                        "token": "main",
                        "name": "Main stream",
                        "uri": f"rtsp://{address}/main",
                        "width": 1280,
                        "height": 720,
                        "encoding": "H264",
                        "fps": 15,
                        "bitrate_kbps": 0,
                        "source_kind": "manual_rtsp",
                    }
                ],
                {"record": "main", "detect": "main"},
            )
        probe_source.return_value = ProbeResult(
            "ready", 20, "h264", None, 1280, 720, 15
        )
        inventory = self._inventory(
            [
                {
                    "candidate_uuid": "candidate-batch-1",
                    "ip": "192.0.2.21",
                    "mac": "02:00:00:00:10:01",
                    "status": "online",
                },
                {
                    "candidate_uuid": "candidate-batch-2",
                    "ip": "192.0.2.31",
                    "mac": "02:00:00:00:10:02",
                    "status": "online",
                },
            ]
        )

        recovered = recover_inventory_addresses(self.repository, inventory)

        self.assertEqual([result.status for result in recovered], ["recovered", "recovered"])
        replace_streams.assert_called_once()
        runtime_sources = replace_streams.call_args.args[0]
        self.assertEqual(len(runtime_sources), 2)
        self.assertEqual(
            {source["uri"] for source in runtime_sources},
            {"rtsp://192.0.2.21/main", "rtsp://192.0.2.31/main"},
        )
        self.assertEqual(probe_source.call_count, 2)
        self.probe_streams.assert_called_once()
        restart_events = [
            item
            for item in self.repository.due_notifications()
            if item["event_type"] == "relay_restarted"
        ]
        self.assertEqual(len(restart_events), 1)
        self.assertEqual(restart_events[0]["payload"]["camera_count"], 2)

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
        adoption = self.repository.adopt(
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
        history = self.repository.camera_identity_history(adoption["camera_uuid"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["ip"], "192.168.1.20")

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
    def test_failed_relay_restart_rolls_back_sources_and_keeps_incident_open(
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
        replace_streams.side_effect = [RuntimeError("synthetic replacement failure"), None]
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
        opened = self.repository.incidents(status="open")
        self.assertEqual(opened["open_count"], 1)
        self.assertEqual(opened["incidents"][0]["kind"], "camera_address_changed")

    @patch("camadmiral.recovery.replace_streams")
    @patch("camadmiral.recovery.probe_source")
    def test_failed_downstream_verification_rolls_back_and_keeps_incident_open(
        self,
        probe_source,
        replace_streams,
    ) -> None:
        self.repository.adopt(
            {"candidate_uuid": "candidate-relay-probe", "display_name": "Camera"},
            "operator",
            "synthetic-secret",
            [
                {
                    "token": "manual-stream",
                    "name": "Stream",
                    "uri": "rtsp://192.0.2.20/live",
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
        probe_source.return_value = ProbeResult(
            "ready", 20, "h264", None, 640, 360, 10
        )
        self.probe_streams.side_effect = lambda sources, *_args: {
            source["stream_uuid"]: ProbeResult("unavailable", 20)
            for source in sources
        }
        inventory = self._inventory(
            {
                "candidate_uuid": "candidate-relay-probe",
                "ip": "192.0.2.99",
                "mac": "02:00:00:00:00:99",
                "status": "online",
            }
        )

        result = recover_inventory_addresses(self.repository, inventory)
        after = self.repository.adoption_for_candidate("candidate-relay-probe")

        self.assertEqual(result[0].status, "runtime_failed")
        self.assertEqual(after["streams"][0]["uri"], "rtsp://192.0.2.20/live")
        self.assertEqual(replace_streams.call_count, 2)
        self.assertEqual(self.probe_streams.call_count, 3)
        opened = self.repository.incidents(status="open")
        self.assertEqual(opened["open_count"], 1)
        self.assertEqual(opened["incidents"][0]["kind"], "camera_address_changed")

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
        history = self.repository.camera_identity_history(before["camera_uuid"])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["ip"], "192.168.1.77")
        self.assertEqual(history[0]["mac"], "02:00:00:00:00:03")
        self.assertEqual(history[0]["onvif_identity"], "urn:uuid:synthetic-camera")
        self.assertEqual(self.repository.incidents(status="open")["open_count"], 0)
        self.assertEqual(
            {incident["kind"] for incident in self.repository.incidents(status="resolved")["incidents"]},
            {"authentication_failed", "camera_address_changed"},
        )
        self.assertTrue(history[0]["current"])
        self.assertEqual(history[1]["ip"], "192.168.1.20")
        self.assertFalse(history[1]["current"])


if __name__ == "__main__":
    unittest.main()
