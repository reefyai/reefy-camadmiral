import threading
import json
import time
import unittest
from unittest.mock import Mock, patch

from camadmiral.media import RelayHealthMonitor, RelayRuntimeActivityMonitor, SnapshotBusyError


class SnapshotHealthTests(unittest.TestCase):
    def source(self, name):
        return dict(stream_uuid=name, camera_uuid='camera-' + name,
                    stream_key='stream_' + name, uri='rtsp://synthetic.invalid/' + name)

    def pending_monitor(self):
        from concurrent.futures import Future
        repo = Mock()
        repo.auth_retry_schedule.return_value = {}
        repo.managed_stream_sources.return_value = [self.source('one')]
        monitor = RelayHealthMonitor()
        monitor._snapshot_executor = Mock()
        monitor._snapshot_executor.submit.return_value = Future()
        return monitor, repo

    def runtime(self, user_agent):
        return json.dumps({'stream_one': {
            'consumers': [{'id': 9, 'user_agent': user_agent}],
            'producers': [{'id': 7, 'bytes_recv': 1000, 'receivers': [
                {'codec': {'codec_type': 'video'}, 'packets': 20}]}],
        }}).encode()

    def test_pending_snapshot_does_not_hide_stalled_real_viewer(self):
        monitor, repo = self.pending_monitor()
        with patch('camadmiral.media._request', return_value=self.runtime('Synthetic-Viewer')):
            self.assertEqual(monitor.probe(repo)['one'].status, 'ready')
            self.assertEqual(monitor.probe(repo)['one'].status, 'unavailable')

    def test_snapshot_rtp_is_not_proof_of_decoded_frame(self):
        monitor, repo = self.pending_monitor()
        with patch('camadmiral.media._request', return_value=self.runtime('CamAdmiral-Snapshot')):
            self.assertEqual(monitor.probe(repo), {})

    def test_waiting_snapshot_does_not_trigger_early_address_recovery(self):
        repo = Mock()
        repo.auth_retry_schedule.return_value = {}
        repo.managed_stream_runtime_sources.return_value = [self.source('one')]
        monitor = RelayRuntimeActivityMonitor(stall_threshold=5)
        with patch('camadmiral.media._request', return_value=self.runtime('CamAdmiral-Snapshot')), \
             patch('camadmiral.media.time.monotonic', side_effect=[0, 20]):
            self.assertEqual(monitor.poll(repo), set())
            self.assertEqual(monitor.poll(repo), set())

    @patch('camadmiral.media._request', return_value=b'{}')
    def test_slow_snapshot_does_not_block_other_streams_or_queue_unbounded_work(self, request):
        release = threading.Event()
        entered = threading.Event()
        sources = [self.source(str(i)) for i in range(8)]
        repo = Mock()
        repo.auth_retry_schedule.return_value = {}
        repo.managed_stream_sources.return_value = sources
        monitor = RelayHealthMonitor()

        def snapshot(key, **kwargs):
            if key == 'stream_0':
                entered.set()
                release.wait(5)
            return b'\xff\xd8\xffframe\xff\xd9'

        try:
            with patch('camadmiral.media.snapshot_frame', side_effect=snapshot):
                start = time.monotonic()
                monitor.probe(repo)
                self.assertLess(time.monotonic() - start, 1)
                self.assertTrue(entered.wait(1))
                seen = set()
                for _ in range(30):
                    results = monitor.probe(repo)
                    seen.update(results)
                    self.assertNotIn('0', results)
                    self.assertLessEqual(len(monitor._pending_snapshots), 4)
                    if '7' in seen:
                        break
                    time.sleep(.01)
                self.assertIn('7', seen, 'Slow first stream starved other cameras')
                self.assertFalse(release.is_set())
        finally:
            release.set()
            monitor._snapshot_executor.shutdown(wait=True)

    @patch('camadmiral.media._request', return_value=b'{}')
    def test_discard_inflight_result_after_source_change(self, request):
        from concurrent.futures import Future
        source = self.source('one')
        repo = Mock()
        repo.auth_retry_schedule.return_value = {}
        repo.managed_stream_sources.return_value = [source]
        monitor = RelayHealthMonitor()
        future = Future()
        monitor._snapshot_executor = Mock()
        monitor._snapshot_executor.submit.return_value = future
        monitor.probe(repo)
        source['uri'] = 'rtsp://synthetic.invalid/new'
        future.set_exception(RuntimeError('old source failed'))
        self.assertEqual(monitor.probe(repo), {})
        self.assertIsNone(monitor.cached_frame('camera-one'))

    @patch('camadmiral.media._request', return_value=b'{}')
    def test_busy_snapshot_worker_is_not_camera_failure(self, request):
        from concurrent.futures import Future
        repo = Mock()
        repo.auth_retry_schedule.return_value = {}
        repo.managed_stream_sources.return_value = [self.source('one')]
        monitor = RelayHealthMonitor()
        future = Future()
        future.set_exception(SnapshotBusyError('capacity exhausted'))
        monitor._snapshot_executor = Mock()
        monitor._snapshot_executor.submit.return_value = future
        self.assertEqual(monitor.probe(repo), {})
        self.assertIn('one', monitor._frame_probe_retries)
        self.assertEqual(monitor._failure_samples, {})
