import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from concurrent.futures import Future

from camadmiral.media import ProbeResult, RelayHealthMonitor, _validate_auth_video, SnapshotError
from camadmiral.storage import CameraRepository


class AuthRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = CameraRepository(Path(self.temp.name) / 'state.db', b'k' * 32)
        self.repo.migrate()
        self.camera = self.repo.adopt(
            {'candidate_uuid': 'synthetic', 'ip': '192.168.1.2'}, 'test', 'secret',
            [{'token': 'main', 'name': 'Main', 'uri': 'rtsp://192.168.1.2/main',
              'width': 640, 'height': 360, 'encoding': 'H264', 'fps': 10, 'bitrate_kbps': 512}],
            {'record': 'main', 'detect': 'main'})
        self.uid = self.camera['roles']['record']

    def test_backoff_is_persistent_and_capped(self):
        with patch('camadmiral.storage.time.time', return_value=1000):
            self.repo.record_probe_results({self.uid: ProbeResult('auth_failed', 1)})
            self.assertFalse(self.repo.claim_auth_retry(self.uid, 1059))
            now = 1060
            for delay in [300, 900, 1800, 1800]:
                self.assertTrue(self.repo.claim_auth_retry(self.uid, now))
                reopened = CameraRepository(self.repo.path, b'k' * 32)
                self.assertFalse(reopened.claim_auth_retry(self.uid, now + delay - 1))
                now += delay

    def test_credentials_and_success_clear_backoff(self):
        self.repo.record_probe_results({self.uid: ProbeResult('auth_failed', 1)})
        self.assertIn(self.uid, self.repo.auth_retry_schedule())
        self.repo.replace_camera_credentials(self.camera['camera_uuid'], 'test', 'new-secret')
        self.assertNotIn(self.uid, self.repo.auth_retry_schedule())
        self.repo.record_probe_results({self.uid: ProbeResult('auth_failed', 1)})
        self.repo.record_probe_results({self.uid: ProbeResult('ready', 1)})
        self.assertNotIn(self.uid, self.repo.auth_retry_schedule())

    def test_existing_stuck_stream_is_seeded_without_changing_identity(self):
        with self.repo.connect() as c:
            c.execute("UPDATE managed_streams SET health_status='auth_failed' WHERE stream_uuid=?", (self.uid,))
            c.commit()
        with patch('camadmiral.storage.time.time', return_value=1000):
            self.assertEqual(self.repo.auth_retry_schedule()[self.uid], 1060)
        self.assertEqual(self.repo.adoption_for_candidate('synthetic')['roles'], self.camera['roles'])

    def pending(self):
        self.repo.record_probe_results({self.uid: ProbeResult('auth_failed', 1)})
        with self.repo.connect() as c:
            c.execute('UPDATE stream_auth_retries SET next_attempt=0')
            c.commit()
        monitor = RelayHealthMonitor()
        monitor._auth_executor = Mock()
        future = Future()
        monitor._auth_executor.submit.return_value = future
        monitor._retry_authentication(self.repo)
        return monitor, future

    def test_one_pending_attempt_and_success_resolves_incident(self):
        monitor, future = self.pending()
        for _ in range(5):
            monitor._retry_authentication(self.repo)
        monitor._auth_executor.submit.assert_called_once()
        future.set_result(None)
        monitor._retry_authentication(self.repo)
        self.assertEqual(self.repo.auth_retry_schedule(), {})
        with self.repo.connect() as c:
            self.assertEqual(c.execute('SELECT health_status FROM managed_streams').fetchone()[0], 'healthy')
            self.assertEqual(c.execute('SELECT count(*) FROM camera_incidents WHERE resolved_at IS NULL').fetchone()[0], 0)

    def test_failed_decode_keeps_auth_state_and_backoff(self):
        monitor, future = self.pending()
        future.set_exception(RuntimeError('no decoded video'))
        monitor._retry_authentication(self.repo)
        monitor._auth_executor.submit.assert_called_once()
        with self.repo.connect() as c:
            self.assertEqual(c.execute('SELECT health_status FROM managed_streams').fetchone()[0], 'auth_failed')

    def test_credential_edit_discards_inflight_success(self):
        monitor, future = self.pending()
        self.repo.replace_camera_credentials(self.camera['camera_uuid'], 'test', 'new-secret')
        future.set_result(None)
        monitor._retry_authentication(self.repo)
        with self.repo.connect() as c:
            self.assertEqual(c.execute('SELECT health_status FROM managed_streams').fetchone()[0], 'unknown')

    def test_bounded_workers_and_one_stream_per_camera(self):
        repo = Mock()
        sources = [dict(stream_uuid=str(i), camera_uuid=str(i // 2),
                        stream_key='stream_' + str(i), uri='rtsp://synthetic.invalid/live')
                   for i in range(16)]
        repo.auth_retry_schedule.return_value = {s['stream_uuid']: 0 for s in sources}
        repo.managed_stream_sources.return_value = sources
        repo.claim_auth_retry.return_value = True
        monitor = RelayHealthMonitor()
        monitor._auth_executor = Mock()
        monitor._auth_executor.submit.side_effect = lambda *args: Future()
        for _ in range(3):
            monitor._retry_authentication(repo)
        self.assertEqual(monitor._auth_executor.submit.call_count, 4)
        self.assertEqual(len(monitor._pending_auth), 4)

    def test_no_recovery_without_decoded_jpeg(self):
        source = dict(uri='rtsp://synthetic.invalid/live', username='test', password='secret')
        with patch('camadmiral.media._snapshot_jpeg', return_value=b''):
            with self.assertRaises(SnapshotError):
                _validate_auth_video(source)

    def test_disabled_camera_discards_inflight_success(self):
        monitor, future = self.pending()
        with self.repo.connect() as c:
            c.execute('UPDATE cameras SET enabled=0')
            c.commit()
        future.set_result(None)
        monitor._retry_authentication(self.repo)
        with self.repo.connect() as c:
            self.assertEqual(c.execute('SELECT health_status FROM managed_streams').fetchone()[0], 'auth_failed')

    def test_simultaneous_sibling_failures_each_get_diagnosed(self):
        repo = Mock()
        repo.auth_retry_schedule.return_value = {}
        sources = [dict(stream_uuid=str(i), camera_uuid='same-camera', stream_key='stream_' + str(i),
                        uri='rtsp://synthetic.invalid/' + str(i), username='test', password='secret')
                   for i in range(2)]
        repo.managed_stream_sources.return_value = sources
        monitor = RelayHealthMonitor()
        failures = {s['stream_uuid']: ProbeResult('unavailable', 1) for s in sources}
        with patch('camadmiral.media._request', return_value=b'{}'), \
             patch.object(monitor, '_sample_periodic_sources', return_value=failures), \
             patch('camadmiral.media.probe_source', return_value=ProbeResult('auth_failed', 1)) as probe:
            for _ in range(3):
                monitor.probe(repo)
        self.assertEqual([call.args[0] for call in probe.call_args_list], [s['uri'] for s in sources])
