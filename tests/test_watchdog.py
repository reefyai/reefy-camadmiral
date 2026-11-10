import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from camadmiral.media import SnapshotBusyError, SnapshotError
from camadmiral.watchdog import StalledVideoWatchdog


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repository = Mock()
        self.repository.path = Path(self.temp.name)/'synthetic.db'
        self.source = {'stream_uuid':'synthetic-stream','stream_key':'stream_synthetic','camera_uuid':'synthetic-camera'}
        self.repository.managed_stream_runtime_sources.return_value=[self.source]
        self.repository.rtsp_access_password.return_value='synthetic-secret'
        self.runtime={'stream_synthetic':{'producers':[{'id':7,'bytes_recv':100,
            'receivers':[{'codec':{'codec_type':'video'},'packets':0},
                         {'codec':{'codec_type':'audio'},'packets':10}]}],
            'consumers':[{'user_agent':'Synthetic recorder'}]}}
        self.watchdog=StalledVideoWatchdog()
        self.now=100.0
        for name in ('monotonic','time'):
            p=patch('camadmiral.watchdog.time.'+name,side_effect=lambda:self.now)
            p.start();self.addCleanup(p.stop)
        p=patch('camadmiral.watchdog._request',side_effect=lambda *_:json.dumps(self.runtime).encode())
        p.start();self.addCleanup(p.stop)
        p=patch('camadmiral.watchdog.snapshot_frame',side_effect=[SnapshotError('synthetic failure'),b'jpeg'])
        self.frame=p.start();self.addCleanup(p.stop)
        p=patch('camadmiral.watchdog.restart_relay')
        self.restart=p.start();self.addCleanup(p.stop)
        self.logs=io.StringIO()
        c=contextlib.redirect_stdout(self.logs);c.__enter__();self.addCleanup(c.__exit__,None,None,None)

    def cycle(self, advance=0, traffic=True):
        self.now+=advance
        if traffic:
            for stream in self.runtime.values():
                stream['producers'][0]['bytes_recv']+=100
        return self.watchdog.cycle(self.repository)

    def trigger(self):
        self.cycle();return self.cycle(21)

    def test_live_audio_without_video_restarts_and_decodes(self):
        self.assertTrue(self.trigger())
        self.restart.assert_called_once_with()
        self.assertEqual(self.frame.call_count,2)
        self.repository.record_probe_results.assert_called_once()
        self.assertIn('recovery_verified',self.logs.getvalue())
        self.assertNotIn('synthetic-secret',self.logs.getvalue())
        self.assertNotIn('rtsp://',self.logs.getvalue())

    def test_shared_reset_does_not_change_sources_or_configuration(self):
        self.trigger()
        self.repository.managed_stream_sources.assert_not_called()
        self.repository.update_profile_sources.assert_not_called()
        self.repository.set_stream_settings.assert_not_called()
        self.repository.enqueue_relay_restart_notification.assert_called_once_with(
            reason='stalled_video_recovery',camera_count=1)

    def test_no_reset_for_powered_off_camera(self):
        self.cycle();self.cycle(21,traffic=False)
        self.restart.assert_not_called();self.frame.assert_not_called()

    def test_no_reset_before_stall_deadline(self):
        self.cycle();self.cycle(19)
        self.restart.assert_not_called()

    def test_video_advancement_resets_stall_deadline(self):
        self.cycle()
        self.runtime['stream_synthetic']['producers'][0]['receivers'][0]['packets']=1
        self.cycle(21);self.cycle(19)
        self.restart.assert_not_called()

    def test_generation_change_does_not_inherit_stall(self):
        self.cycle()
        self.runtime['stream_synthetic']['producers'][0]['id']=8
        self.cycle(21);self.restart.assert_not_called()

    def test_idle_and_snapshot_only_streams_are_not_restarted(self):
        for consumers in ([],[{'user_agent':'CamAdmiral-Snapshot'}]):
            self.runtime['stream_synthetic']['consumers']=consumers
            self.trigger()
        self.restart.assert_not_called()

    def test_disabled_or_removed_stream_is_not_restarted(self):
        self.cycle();self.repository.managed_stream_runtime_sources.return_value=[]
        self.cycle(21);self.restart.assert_not_called()

    def test_decodable_frame_prevents_counter_based_reset(self):
        self.frame.side_effect=None;self.frame.return_value=b'jpeg'
        self.assertFalse(self.trigger());self.restart.assert_not_called()

    def test_capacity_pressure_is_not_camera_failure(self):
        self.frame.side_effect=SnapshotBusyError('busy')
        self.assertFalse(self.trigger());self.restart.assert_not_called()
        self.assertFalse((Path(self.temp.name)/'relay-watchdog.json').exists())

    def test_restart_failure_consumes_budget_and_never_claims_recovery(self):
        self.restart.side_effect=RuntimeError('rtsp://operator:secret@192.0.2.1/live')
        self.assertFalse(self.trigger())
        self.assertIn('restart_failed',self.logs.getvalue())
        self.assertNotIn('recovery_verified',self.logs.getvalue())
        self.assertNotIn('operator',self.logs.getvalue())
        self.assertFalse(self.cycle(21));self.restart.assert_called_once()

    def test_budget_survives_new_watchdog_object_and_is_private(self):
        self.trigger();self.watchdog=StalledVideoWatchdog()
        self.cycle(1);self.cycle(21)
        self.restart.assert_called_once()
        self.assertEqual((Path(self.temp.name)/'relay-watchdog.json').stat().st_mode&0o777,0o600)

    def test_budget_caps_shared_resets_per_hour(self):
        p=Path(self.temp.name)/'relay-watchdog.json'
        for now in (1000,1120,1240):self.assertEqual(self.watchdog._reserve_restart(p,now),(True,0))
        self.assertEqual(self.watchdog._reserve_restart(p,1500),(False,3100))
        self.assertEqual(self.watchdog._reserve_restart(p,4600),(True,0))

    def test_corrupt_budget_fails_closed(self):
        (Path(self.temp.name)/'relay-watchdog.json').write_text('invalid')
        self.assertFalse(self.trigger());self.restart.assert_not_called();self.frame.assert_not_called()

    def test_backwards_wall_clock_cannot_reset_budget(self):
        p=Path(self.temp.name)/'relay-watchdog.json'
        self.watchdog._reserve_restart(p,1000)
        allowed,wait=self.watchdog._reserve_restart(p,900)
        self.assertFalse(allowed);self.assertEqual(wait,220)

    def test_undecodable_recovery_is_not_reported_healthy(self):
        self.frame.side_effect=SnapshotError('no video')
        self.assertTrue(self.trigger())
        self.repository.record_probe_results.assert_not_called()
        self.assertNotIn('recovery_verified',self.logs.getvalue())
        self.cycle(10);self.cycle(10)
        self.assertFalse(self.watchdog._verification)
        self.restart.assert_called_once()

    def test_verification_is_cancelled_after_disable(self):
        self.frame.side_effect=[SnapshotError('no video'),SnapshotBusyError('busy')]
        self.trigger()
        self.repository.managed_stream_runtime_sources.return_value=[]
        self.cycle(10)
        self.repository.record_probe_results.assert_not_called()
        self.assertIn('verification_cancelled',self.logs.getvalue())

    def test_busy_verification_is_retried_without_another_reset(self):
        self.frame.side_effect=[SnapshotError('no video'),SnapshotBusyError('busy'),b'jpeg']
        self.trigger();self.cycle(6)
        self.restart.assert_called_once();self.repository.record_probe_results.assert_called_once()

    def test_grouped_stalls_use_one_reset_and_verify_each_stream(self):
        second={**self.source,'stream_uuid':'synthetic-second','stream_key':'stream_second'}
        self.repository.managed_stream_runtime_sources.return_value=[self.source,second]
        self.runtime['stream_second']=json.loads(json.dumps(self.runtime['stream_synthetic']))
        self.frame.side_effect=[SnapshotError('no video'),b'jpeg',b'jpeg']
        self.trigger();self.cycle(5)
        self.restart.assert_called_once()
        self.assertEqual(self.repository.record_probe_results.call_count,2)

    def test_notification_failure_does_not_hide_successful_video_recovery(self):
        self.repository.enqueue_relay_restart_notification.side_effect=RuntimeError('synthetic notification error')
        self.assertTrue(self.trigger())
        self.repository.record_probe_results.assert_called_once()
        self.assertIn('notification_failed',self.logs.getvalue())
        self.assertIn('recovery_verified',self.logs.getvalue())

    def test_deferred_budget_does_not_repeat_slow_snapshot_probes(self):
        self.trigger();self.watchdog=StalledVideoWatchdog()
        self.cycle();self.cycle(21);self.cycle(2)
        self.assertEqual(self.frame.call_count,2)
        self.assertEqual(self.logs.getvalue().count('"reason": "restart_budget"'),1)


if __name__=='__main__':unittest.main()
