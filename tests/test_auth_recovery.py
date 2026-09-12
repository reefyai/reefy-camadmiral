import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from camadmiral.media import ProbeResult, RelayHealthMonitor, SnapshotError
from camadmiral.storage import CameraRepository


class AuthenticationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = CameraRepository(Path(self.tmp.name) / "camera.db", b"k" * 32)
        self.repo.migrate()
        self.adoption = self.repo.adopt(
            {"candidate_uuid": "synthetic-camera", "display_name": "Test camera"},
            "operator", "synthetic-secret",
            [{"token": token, "name": token, "uri": f"rtsp://192.0.2.10/{token}",
              "width": 640, "height": 360, "encoding": "H264", "fps": 10,
              "bitrate_kbps": 512} for token in ("main", "sub")],
            {"record": "main", "detect": "sub"},
        )
        self.sources = self.repo.managed_stream_sources(role_bound_only=True)
        self.repo.record_probe_results({s["stream_uuid"]: ProbeResult("auth_failed", 1) for s in self.sources})
        failure = self.repo.managed_stream_sources()[0]["last_failure_at"]
        self.wall = patch("camadmiral.media.time.time", return_value=datetime.fromisoformat(failure).timestamp()).start()
        self.clock = patch("camadmiral.media.time.monotonic", return_value=100.0).start()
        self.frame = patch("camadmiral.media.snapshot_frame", return_value=b"synthetic-jpeg").start()
        self.direct = patch("camadmiral.media.probe_source", return_value=ProbeResult("auth_failed", 1)).start()
        self.runtime = {}
        patch("camadmiral.media._request", side_effect=lambda *_a: json.dumps(self.runtime).encode()).start()
        self.addCleanup(patch.stopall)
        self.monitor = RelayHealthMonitor()

    def active(self, source, packets=10):
        self.runtime[source["stream_key"]] = {
            "producers": [{"id": 1, "bytes_recv": 100, "receivers": [
                {"codec": {"codec_type": "video"}, "packets": packets}]}],
            "consumers": [{"id": 2}],
        }

    def test_advancing_media_clears_auth_and_incident_without_new_connections(self):
        for source in self.sources:
            self.active(source)
        self.assertEqual(self.monitor.probe(self.repo), {})
        for source in self.sources:
            self.active(source, 11)
        results = self.monitor.probe(self.repo)
        self.assertEqual({r.status for r in results.values()}, {"ready"})
        self.frame.assert_not_called()
        self.direct.assert_not_called()
        self.assertEqual({s["health_status"] for s in self.repo.managed_stream_sources()}, {"healthy"})
        with self.repo.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM camera_incidents WHERE resolved_at IS NULL").fetchone()[0], 0)
        restored = self.repo.adoption_for_candidate("synthetic-camera")
        self.assertEqual(restored["camera_uuid"], self.adoption["camera_uuid"])
        self.assertEqual({s["stream_key"] for s in restored["streams"]}, {s["stream_key"] for s in self.adoption["streams"]})

    def test_frozen_or_missing_counters_do_not_clear_auth(self):
        for packets in (0, 10, 10, 10):
            for source in self.sources:
                self.active(source, packets)
            self.assertEqual(self.monitor.probe(self.repo), {})
        self.assertEqual({s["health_status"] for s in self.repo.managed_stream_sources()}, {"auth_failed"})
        self.frame.assert_not_called()
        self.direct.assert_not_called()

    def test_idle_retry_cooldown_backoff_cap_and_one_stream_per_camera(self):
        self.frame.side_effect = SnapshotError("unavailable")
        self.monitor.probe(self.repo)
        camera = self.sources[0]["camera_uuid"]
        for before, due, next_due in ((399, 400, 1000), (999, 1000, 2200), (2199, 2200, 4000), (3999, 4000, 5800)):
            count = self.frame.call_count
            self.clock.return_value = before
            self.monitor.probe(self.repo)
            self.assertEqual(self.frame.call_count, count)
            self.clock.return_value = due
            self.monitor.probe(self.repo)
            self.assertEqual(self.frame.call_count, count + 1)
            self.assertEqual(self.monitor._auth_retry_at[camera], next_due)
        self.assertEqual(len({c.args[0] for c in self.frame.call_args_list}), 2)
        self.assertEqual({s["health_status"] for s in self.repo.managed_stream_sources()}, {"auth_failed"})
        self.direct.assert_not_called()

    def test_idle_recovery_only_clears_tested_stream(self):
        self.monitor.probe(self.repo)
        self.clock.return_value = 400
        result = self.monitor.probe(self.repo)
        self.assertEqual(len(result), 1)
        self.assertEqual(next(iter(result.values())).status, "ready")
        self.assertEqual(sorted(s["health_status"] for s in self.repo.managed_stream_sources()), ["auth_failed", "healthy"])
        self.direct.assert_not_called()

    def test_restart_respects_recent_failure_cooldown(self):
        self.wall.return_value += 120
        self.monitor.probe(self.repo)
        self.assertEqual(self.monitor._auth_retry_at[self.sources[0]["camera_uuid"]], 280)
        self.frame.assert_not_called()

    def test_restart_after_failed_retry_does_not_immediately_retry_again(self):
        self.frame.side_effect = SnapshotError("unavailable")
        self.monitor.probe(self.repo)
        self.clock.return_value = 400
        self.monitor.probe(self.repo)
        self.assertEqual(self.frame.call_count, 1)
        self.clock.return_value = 401
        restarted = RelayHealthMonitor()
        restarted.probe(self.repo)
        self.assertEqual(self.frame.call_count, 1)
        self.assertGreaterEqual(restarted._auth_retry_at[self.sources[0]["camera_uuid"]], 700)

    def test_auth_diagnostic_does_not_poison_healthy_sibling(self):
        self.repo.record_probe_results({s["stream_uuid"]: ProbeResult("ready", 1) for s in self.sources})
        healthy, broken = self.sources
        self.active(healthy)
        self.runtime[broken["stream_key"]] = {"producers": [], "consumers": [{"id": 3}]}
        self.frame.side_effect = SnapshotError("unavailable")
        self.monitor.probe(self.repo)
        self.active(healthy, 11)
        result = self.monitor.probe(self.repo)
        self.assertEqual(result[healthy["stream_uuid"]].status, "ready")
        self.assertEqual(result[broken["stream_uuid"]].status, "auth_failed")
        self.assertEqual({s["stream_uuid"]: s["health_status"] for s in self.repo.managed_stream_sources()}[healthy["stream_uuid"]], "healthy")
