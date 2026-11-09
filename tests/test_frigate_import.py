"""Import boundaries, encrypted recovery, and ownership preservation."""
import copy
import http.client
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camadmiral.frigate import FrigateTarget, reconcile_frigate, desired_resource_keys
from camadmiral.frigate_import import (ImportStore, preview, public_job, resolve_source,
                                      execute, restore, sync_imported, inspect_camera, start_job, recover_handoffs)
from camadmiral.frigate import FrigateApiError, FrigateClient
from camadmiral.media import ProbeResult
from camadmiral.storage import CameraRepository
from test_frigate import FakeFrigateClient


class ImportClient(FakeFrigateClient):
    def __init__(self, target):
        super().__init__(target)
        self.current_raw_config = {'cameras': {}, 'go2rtc': {'streams': {}}}
        self.current_raw_paths = copy.deepcopy(self.current_raw_config)

    def seed(self, key, camera, streams=None):
        for cfg in (self.current_config, self.current_raw_config, self.current_raw_paths):
            cfg.setdefault('cameras', {})[key] = copy.deepcopy(camera)
            cfg.setdefault('go2rtc', {}).setdefault('streams', {}).update(copy.deepcopy(streams or {}))
        self.current_stats[key] = {'camera_fps': 5}

    def raw_config(self):
        return copy.deepcopy(self.current_raw_config)

    def restart(self):
        super().restart()
        self.current_config = copy.deepcopy(self.current_raw_config)
        self.current_raw_paths = copy.deepcopy(self.current_raw_config)
        for key, camera in self.current_config['cameras'].items():
            self.current_stats[key] = {'camera_fps': 5 if camera.get('enabled', True) else 0}

    def runtime_streams(self):
        return {name: {'producers': [{'bytes_recv': 1}], 'consumers': [{'id': 'synthetic-reader'}]}
                for name in self.current_raw_paths.get('go2rtc', {}).get('streams', {})}

    def set_config(self, config_data, **kwargs):
        def check_deletes(current, changes):
            current = current if isinstance(current, dict) else {}
            for key, value in changes.items():
                if isinstance(value, dict):
                    check_deletes(current.get(key, {}), value)
                elif value == '' and key not in current:
                    # Frigate 0.17 deletes with del, rather than pop(..., None).
                    raise FrigateApiError('request_rejected', upstream_status=500)
        check_deletes(self.current_raw_config, config_data)
        return super().set_config(config_data, **kwargs)


class FrigateImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = CameraRepository(Path(self.tmp.name) / 'camera.db', b'x' * 32)
        self.repo.migrate()
        self.repo.save_frigate_target('synthetic-target', 'Synthetic target', 'http://frigate:5000')
        self.target = FrigateTarget('synthetic-target', 'Synthetic target', 'http://frigate:5000')
        self.client = ImportClient(self.target)
        self.source_runtime = self.enterContext(patch('camadmiral.frigate_import.source_connection_revisions', create=True, return_value={}))
        self.camera = {'friendly_name': 'Original entrance', 'enabled': True,
                       'ffmpeg': {'inputs': [{'path': 'rtsp://operator:synthetic-password@192.0.2.10/main',
                                             'roles': ['record', 'detect']}]},
                       'detect': {'width': 640, 'height': 360, 'fps': 3},
                       'record': {'enabled': True, 'continuous': {'days': 7}},
                       'zones': {}, 'motion': {'mask': '0,0,0,1,1,1'},
                       'onvif': {'host': '192.0.2.10', 'user': 'control', 'password': 'synthetic-onvif-password'}}
        self.client.seed('legacy_entrance', self.camera)

    def preview(self):
        return preview(self.repo, self.target, self.client)

    def capped(self, job, *, after_release='ready', snapshot=None, adopt=None):
        def probe(uri, *credentials):
            if '192.0.2.10' in uri:
                status = 'unavailable' if self.client.config()['cameras']['legacy_entrance'].get('enabled', True) else after_release
                if isinstance(status, BaseException):
                    raise status
                return ProbeResult(status, 1, 'h264', width=640, height=360, fps=10)
            return ProbeResult('ready', 1, 'h264', width=640, height=360, fps=10)
        with patch('camadmiral.frigate_import.probe_source', side_effect=probe), \
             patch('camadmiral.frigate_import._target_host', return_value='192.0.2.20'), \
             patch('camadmiral.frigate_import.snapshot_frame', side_effect=snapshot, return_value=b'frame'):
            return execute(self.repo, self.target, job['id'], ['legacy_entrance'], [], None, lambda: True, self.client)

    def slow_restart(self, clock, seconds=110):
        restart, config = self.client.restart, self.client.config
        unavailable_until = [0]
        def delayed_restart():
            restart()
            unavailable_until[0] = clock[0] + seconds
        def delayed_config():
            if clock[0] < unavailable_until[0]:
                raise FrigateApiError('target_unavailable')
            return config()
        return patch.object(self.client, 'restart', side_effect=delayed_restart), patch.object(self.client, 'config', side_effect=delayed_config)

    def test_slow_restart_handoff_and_restoration_wait_for_new_ready_instance(self):
        clock = [0.0]
        def sleep(seconds): clock[0] += seconds
        restart, config = self.slow_restart(clock)
        with patch('camadmiral.frigate_import.time.monotonic', side_effect=lambda: clock[0]), \
             patch('camadmiral.frigate_import.time.sleep', side_effect=sleep), restart, config:
            job = self.capped(self.preview())
            self.assertEqual('applied', job['cameras'][0]['status'])
            restore(self.repo, self.target, 'legacy_entrance', self.client, lambda: True)
        self.assertGreaterEqual(clock[0], 330)
        self.assertEqual(self.camera, self.client.raw_config()['cameras']['legacy_entrance'])
        self.assertEqual({}, ImportStore(self.repo).links(self.target.target_id))

    def test_restore_retries_temporary_outage_and_preserves_operator_conflict(self):
        class ProcessLost(BaseException): pass
        job = self.preview()
        with self.assertRaises(ProcessLost): self.capped(job, after_release=ProcessLost())
        raw = self.client.raw_config
        for conflict in (False, True):
            with self.subTest(conflict=conflict):
                clock = [0.0]
                def unavailable():
                    if clock[0] < 12:
                        raise FrigateApiError('request_rejected', upstream_status=503)
                    return raw()
                def sleep(seconds):
                    clock[0] += seconds
                    if conflict:
                        self.client.current_raw_config['cameras']['legacy_entrance']['detect']['fps'] = 9
                with patch.object(self.client, 'raw_config', side_effect=unavailable), \
                     patch('camadmiral.frigate_import.time.monotonic', side_effect=lambda: clock[0]), \
                     patch('camadmiral.frigate_import.time.sleep', side_effect=sleep):
                    if conflict:
                        with self.assertRaises(FrigateApiError) as error:
                            restore(self.repo, self.target, 'legacy_entrance', self.client)
                        self.assertEqual('rollback_configuration_conflict', error.exception.code)
                        self.assertTrue(ImportStore(self.repo).links(self.target.target_id))
                        self.assertEqual(9, raw()['cameras']['legacy_entrance']['detect']['fps'])
                    else:
                        restore(self.repo, self.target, 'legacy_entrance', self.client)
                        self.assertEqual(self.camera, raw()['cameras']['legacy_entrance'])
                        # Prepare another interrupted transaction for the conflict case.
                if not conflict:
                    with self.assertRaises(ProcessLost): self.capped(self.preview(), after_release=ProcessLost())

    def test_interrupted_restoration_does_not_restart_already_restored_capture(self):
        job = self.capped(self.preview())
        class ProcessLost(BaseException): pass
        restart = self.client.restart
        def lost_reply():
            restart()
            raise ProcessLost()
        with patch.object(self.client, 'restart', side_effect=lost_reply), self.assertRaises(ProcessLost):
            restore(self.repo, self.target, 'legacy_entrance', self.client, lambda: True)
        count, writes = self.client.restart_calls, len(self.client.config_writes)
        recover_handoffs(self.repo, lambda: True, lambda target: self.client)
        self.assertEqual(count, self.client.restart_calls)
        self.assertEqual(writes, len(self.client.config_writes))
        self.assertEqual({}, ImportStore(self.repo).links(self.target.target_id))
        self.assertFalse(self.repo.consumer_inventory()[0]['enabled'])

    def test_persistent_restoration_outage_is_bounded_and_retains_journal(self):
        class ProcessLost(BaseException): pass
        with self.assertRaises(ProcessLost): self.capped(self.preview(), after_release=ProcessLost())
        clock = [0.0]
        def sleep(seconds): clock[0] += seconds
        with patch.object(self.client, 'raw_config', side_effect=FrigateApiError('target_unavailable')), \
             patch('camadmiral.frigate_import.time.monotonic', side_effect=lambda: clock[0]), \
             patch('camadmiral.frigate_import.time.sleep', side_effect=sleep), self.assertRaises(FrigateApiError):
            restore(self.repo, self.target, 'legacy_entrance', self.client)
        self.assertGreaterEqual(clock[0], 110)
        self.assertLessEqual(clock[0], 300)
        self.assertTrue(ImportStore(self.repo).links(self.target.target_id))

    def test_old_instance_positive_capture_cannot_acknowledge_a_restart(self):
        from camadmiral.frigate_import import _restart
        clock = [0.0]
        def sleep(seconds): clock[0] += seconds
        # Model healthy-looking responses from the shutting-down process,
        # including the sub-second rounding drift of Frigate's uptime clock.
        with patch.object(self.client, 'service_started_at', side_effect=lambda: 1000 + min(clock[0] / 1000, 0.75)), \
             patch('camadmiral.frigate_import.time.monotonic', side_effect=lambda: clock[0]), \
             patch('camadmiral.frigate_import.time.sleep', side_effect=sleep), self.assertRaises(FrigateApiError):
            _restart(self.client)
        self.assertEqual(1, self.client.restart_calls)
        self.assertGreaterEqual(clock[0], 110)
        self.assertLessEqual(clock[0], 300)

    def test_restore_does_not_retry_rejected_access_or_invalid_configuration(self):
        class ProcessLost(BaseException): pass
        with self.assertRaises(ProcessLost): self.capped(self.preview(), after_release=ProcessLost())
        for error in (FrigateApiError('authorization_required', upstream_status=403),
                      FrigateApiError('request_rejected', upstream_status=400)):
            with self.subTest(code=error.code), \
                 patch.object(self.client, 'raw_config', side_effect=error) as read, \
                 patch('camadmiral.frigate_import.time.sleep') as sleep, self.assertRaises(FrigateApiError):
                restore(self.repo, self.target, 'legacy_entrance', self.client)
            self.assertEqual(1, read.call_count)
            sleep.assert_not_called()
            self.assertTrue(ImportStore(self.repo).links(self.target.target_id))

    def test_restore_retries_lost_save_acknowledgement_without_losing_originals(self):
        class ProcessLost(BaseException): pass
        with self.assertRaises(ProcessLost): self.capped(self.preview(), after_release=ProcessLost())
        write = self.client.set_config
        interrupted = [False]
        def lost_save(*args, **kwargs):
            write(*args, **kwargs)
            if not interrupted[0]:
                interrupted[0] = True
                raise FrigateApiError('target_unavailable')
        with patch.object(self.client, 'set_config', side_effect=lost_save), \
             patch('camadmiral.frigate_import.time.sleep'):
            restore(self.repo, self.target, 'legacy_entrance', self.client)
        self.assertEqual(self.camera, self.client.raw_config()['cameras']['legacy_entrance'])
        self.assertGreater(self.client.stats()['legacy_entrance']['camera_fps'], 0)
        self.assertEqual({}, ImportStore(self.repo).links(self.target.target_id))

    def test_startup_internal_error_is_retried_only_for_reads(self):
        class ProcessLost(BaseException): pass
        with self.assertRaises(ProcessLost): self.capped(self.preview(), after_release=ProcessLost())
        raw = self.client.raw_config
        with patch.object(self.client, 'raw_config', side_effect=[
                FrigateApiError('request_rejected', upstream_status=500), raw()]), \
             patch('camadmiral.frigate_import.time.sleep'):
            restore(self.repo, self.target, 'legacy_entrance', self.client)
        self.assertEqual({}, ImportStore(self.repo).links(self.target.target_id))
        with self.assertRaises(ProcessLost): self.capped(self.preview(), after_release=ProcessLost())
        with patch.object(self.client, 'set_config', side_effect=FrigateApiError('request_rejected', upstream_status=500)) as write, \
             patch('camadmiral.frigate_import.time.sleep') as sleep, self.assertRaises(FrigateApiError):
            restore(self.repo, self.target, 'legacy_entrance', self.client)
        self.assertEqual(1, write.call_count)
        sleep.assert_not_called()
        self.assertTrue(ImportStore(self.repo).links(self.target.target_id))

    def source_generation_fixture(self):
        camera = copy.deepcopy(self.camera)
        camera['ffmpeg']['inputs'] = [
            {'path': 'rtsp://operator:synthetic-password@192.0.2.10/main', 'roles': ['record']},
            {'path': 'rtsp://operator:synthetic-password@192.0.2.10/sub', 'roles': ['detect']}]
        self.client.seed('legacy_entrance', camera)
        job = self.execute(self.preview())
        adopted = self.repo.consumer_inventory()[0]
        ids = {role: stream['stream_uuid'] for stream in adopted['streams'] for role in stream['roles']}
        self.source_runtime.return_value = {ids['record']: 'source-record-one', ids['detect']: 'source-detect-one'}
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: '192.0.2.20')
        self.client.operations.clear()
        self.client.config_writes.clear()
        return job, adopted, ids

    def test_source_reconnect_refreshes_both_readers_and_acknowledges_post_refresh_connections(self):
        job, adopted, ids = self.source_generation_fixture()
        before = self.client.raw_config()
        self.source_runtime.return_value = {ids['record']: 'source-record-two', ids['detect']: 'source-detect-one'}
        enable = self.client.set_runtime_camera_enabled
        def resumed(key, enabled):
            enable(key, enabled)
            if enabled:
                self.source_runtime.return_value = {ids['record']: 'source-record-three', ids['detect']: 'source-detect-three'}
        with patch.object(self.client, 'set_runtime_camera_enabled', side_effect=resumed):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: '192.0.2.20')
        self.assertIn(('set_runtime_camera_enabled', 'legacy_entrance', False), self.client.operations)
        self.assertIn(('set_runtime_camera_enabled', 'legacy_entrance', True), self.client.operations)
        aliases = ImportStore(self.repo).links(self.target.target_id)['legacy_entrance']['aliases']
        self.assertTrue(set(aliases.values()) <= set(self.client.runtime_deletes))
        self.assertEqual(before, self.client.raw_config())
        self.assertEqual([], self.client.config_writes)
        self.assertEqual(adopted, self.repo.consumer_inventory()[0])
        row = ImportStore(self.repo).links(self.target.target_id)['legacy_entrance']
        self.assertEqual(self.source_runtime.return_value, row['relay_source_revisions'])
        self.client.operations.clear()
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: '192.0.2.20')
        self.assertEqual([], self.client.operations, 'Recovery must not loop when its own pause reconnects a source')

    def test_missing_source_generation_does_not_acknowledge_or_refresh_a_reconnect(self):
        _, _, ids = self.source_generation_fixture()
        before = ImportStore(self.repo).links(self.target.target_id)['legacy_entrance'].get('relay_source_revisions')
        self.source_runtime.return_value = {ids['record']: 'source-record-two'}
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: '192.0.2.20')
        self.assertEqual([], self.client.operations)
        self.assertEqual(before, ImportStore(self.repo).links(self.target.target_id)['legacy_entrance'].get('relay_source_revisions'))
        self.source_runtime.return_value = {ids['record']: 'source-record-two', ids['detect']: 'source-detect-two'}
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: '192.0.2.20')
        self.assertIn(('set_runtime_camera_enabled', 'legacy_entrance', False), self.client.operations)

    def test_source_reconnect_respects_recording_disabled_and_operator_source_edits(self):
        _, _, ids = self.source_generation_fixture()
        self.client.current_config['cameras']['legacy_entrance']['record']['enabled'] = False
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: '192.0.2.20')
        self.client.operations.clear()
        self.source_runtime.return_value = {ids['record']: 'unused-record-connection', ids['detect']: 'source-detect-one'}
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: '192.0.2.20')
        self.assertEqual([], self.client.operations)
        self.client.current_raw_config['cameras']['legacy_entrance']['ffmpeg']['inputs'][0]['path'] = 'rtsp://192.0.2.99/operator-edit'
        self.source_runtime.return_value[ids['detect']] = 'source-detect-two'
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: '192.0.2.20')
        self.assertEqual([], self.client.operations)
        self.assertEqual('rtsp://192.0.2.99/operator-edit', self.client.raw_config()['cameras']['legacy_entrance']['ffmpeg']['inputs'][0]['path'])

    def test_source_refresh_without_verified_capture_uses_guarded_restart_fallback(self):
        _, _, ids = self.source_generation_fixture()
        before, restarts = self.client.raw_config(), self.client.restart_calls
        self.source_runtime.return_value = {ids['record']: 'source-record-two', ids['detect']: 'source-detect-two'}
        clock = [0.0]
        with patch('camadmiral.frigate_import._reader_media_ready', return_value=False), \
             patch('camadmiral.frigate_import.time.monotonic', side_effect=lambda: clock[0]):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: '192.0.2.20')
            self.assertEqual(restarts, self.client.restart_calls)
            self.assertTrue(ImportStore(self.repo).links(self.target.target_id)['legacy_entrance'].get('source_recovery_pending'))
            clock[0] = 91
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: '192.0.2.20')
        self.assertEqual(restarts + 1, self.client.restart_calls)
        self.assertEqual(before, self.client.raw_config())
        self.assertEqual([], self.client.config_writes)
        self.client.operations.clear()
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: '192.0.2.20')
        self.assertEqual([], self.client.operations)

    def test_pending_media_recovery_does_not_block_another_camera(self):
        other = copy.deepcopy(self.camera)
        other['ffmpeg']['inputs'][0]['path'] = 'rtsp://operator:synthetic-password@192.0.2.11/main'
        self.client.seed('second_camera', other)
        self.execute(self.preview(), selected=['legacy_entrance', 'second_camera'])
        self.client.current_stats['legacy_entrance']['camera_fps'] = 0
        self.client.operations.clear()
        with patch('camadmiral.frigate_import.RELAY_RUNTIME_REVISION', 'synthetic-new-epoch'), \
             patch('camadmiral.frigate_import.time.sleep', side_effect=AssertionError('Recovery must yield to the next camera')):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: '192.0.2.20')
        self.assertEqual({'legacy_entrance', 'second_camera'},
                         {op[1] for op in self.client.operations if op[0] == 'set_runtime_camera_enabled'})
        links = ImportStore(self.repo).links(self.target.target_id)
        self.assertTrue(links['legacy_entrance'].get('source_recovery_pending'))
        self.assertNotIn('source_recovery_pending', links['second_camera'])

    def test_saturated_source_is_journaled_before_releasing_and_default_enabled_is_restored(self):
        self.camera.pop('enabled')
        self.client.seed('legacy_entrance', self.camera)
        unselected = copy.deepcopy(self.camera)
        self.client.seed('untouched_camera', unselected)
        states = []
        save = ImportStore.save_link
        def observe(store, target_id, key, row):
            states.append((row.get('state'), row.get('camera_uuid'), len(self.repo.consumer_inventory())))
            return save(store, target_id, key, row)
        with patch.object(ImportStore, 'save_link', autospec=True, side_effect=observe):
            result = self.capped(self.preview())
        self.assertEqual('applied', result['cameras'][0]['status'])
        self.assertIn(('releasing', None, 0), states)
        self.assertIn(('paused', None, 0), states)
        self.assertEqual(2, self.client.restart_calls)
        actual = self.client.raw_config()['cameras']['legacy_entrance']
        self.assertNotIn('enabled', actual)
        for k, value in self.camera.items():
            if k != 'ffmpeg': self.assertEqual(value, actual[k])
        self.assertEqual(unselected, self.client.raw_config()['cameras']['untouched_camera'])
        with self.repo.connect() as c:
            self.assertEqual(0, c.execute('SELECT COUNT(*) FROM frigate_import_handoffs').fetchone()[0])

    def test_offline_source_does_not_trigger_handoff(self):
        self.client.current_stats['legacy_entrance']['camera_fps'] = 0
        result = self.capped(self.preview())
        self.assertEqual('source_unavailable', result['cameras'][0]['reason'])
        self.assertEqual('unchanged', public_job(result)['cameras'][0]['configuration_status'])
        self.assertEqual([], self.client.config_writes)
        self.assertEqual([], self.repo.consumer_inventory())

    def test_restore_default_enabled_handoff_survives_a_second_process_loss(self):
        self.camera.pop('enabled')
        self.client.seed('legacy_entrance', self.camera)
        job = self.capped(self.preview())
        self.assertEqual('applied', job['cameras'][0]['status'])
        self.assertNotIn('enabled', self.client.raw_config()['cameras']['legacy_entrance'])
        class ProcessLost(BaseException): pass
        with patch.object(self.client, 'restart', side_effect=ProcessLost()), self.assertRaises(ProcessLost):
            restore(self.repo, self.target, 'legacy_entrance', self.client, lambda: True)
        self.assertEqual(self.camera, self.client.raw_config()['cameras']['legacy_entrance'])
        self.assertEqual('restored', ImportStore(self.repo).links(self.target.target_id)['legacy_entrance']['state'])
        recover_handoffs(self.repo, lambda: True, lambda target: self.client)
        self.assertEqual(self.camera, self.client.raw_config()['cameras']['legacy_entrance'])
        self.assertFalse(self.repo.consumer_inventory()[0]['enabled'])
        self.assertEqual({}, ImportStore(self.repo).links(self.target.target_id))

    def test_failed_source_after_release_restores_before_adoption(self):
        job = self.capped(self.preview(), after_release='unavailable')
        self.assertEqual('source_unavailable', job['cameras'][0]['reason'])
        self.assertEqual('restored', public_job(job)['cameras'][0]['configuration_status'])
        self.assertEqual('restored', job['cameras'][0]['state'])
        self.assertEqual(self.camera, self.client.raw_config()['cameras']['legacy_entrance'])
        self.assertEqual([], self.repo.consumer_inventory())
        self.assertEqual({}, ImportStore(self.repo).links(self.target.target_id))
        self.assertEqual(2, self.client.restart_calls)

    def test_failed_restoration_retains_recovery_state_without_claiming_success(self):
        with patch('camadmiral.frigate_import.restore', side_effect=FrigateApiError('rollback_configuration_conflict')):
            job = self.capped(self.preview(), after_release='unavailable')
        row = public_job(job)['cameras'][0]
        self.assertEqual('rollback_required', row['reason'])
        self.assertEqual('restore_required', row['configuration_status'])
        self.assertTrue(ImportStore(self.repo).links(self.target.target_id))
        self.assertEqual([], self.repo.consumer_inventory())

    def test_nested_shared_capture_alias_is_never_released(self):
        own = copy.deepcopy(self.camera)
        own['ffmpeg']['inputs'][0]['path'] = 'rtsp://127.0.0.1:8554/outer'
        own['live'] = {'streams': {'Main': 'outer'}}
        self.client.seed('legacy_entrance', own, {'outer': 'rtsp://127.0.0.1:8554/shared', 'shared': self.camera['ffmpeg']['inputs'][0]['path']})
        other = copy.deepcopy(self.camera)
        other['ffmpeg']['inputs'][0]['path'] = 'rtsp://127.0.0.1:8554/shared'
        self.client.seed('other_camera', other)
        job = self.capped(self.preview())
        self.assertEqual('shared_handoff_source', job['cameras'][0]['reason'])
        self.assertEqual([], self.client.config_writes)

    def test_handoff_rollback_releases_new_relay_and_retry_reuses_retained_camera(self):
        from camadmiral.media import SnapshotError
        job = self.capped(self.preview(), snapshot=SnapshotError('unavailable'))
        camera = self.repo.consumer_inventory()[0]
        self.assertFalse(camera['enabled'])
        self.assertEqual(self.camera, self.client.raw_config()['cameras']['legacy_entrance'])
        result = self.execute(job)
        self.assertEqual('applied', result['cameras'][0]['status'])
        self.assertIsNone(public_job(result)['cameras'][0]['configuration_status'])
        self.assertEqual(camera['camera_uuid'], result['cameras'][0]['camera_uuid'])
        self.assertTrue(self.repo.consumer_inventory()[0]['enabled'])
        restore(self.repo, self.target, 'legacy_entrance', self.client, lambda: True)
        self.assertFalse(self.repo.consumer_inventory()[0]['enabled'])

    def test_startup_recovers_encrypted_pre_adoption_handoff_after_process_loss(self):
        class ProcessLost(BaseException): pass
        job = self.preview()
        with self.assertRaises(ProcessLost):
            self.capped(job, after_release=ProcessLost())
        store = ImportStore(self.repo)
        self.assertFalse(self.client.config()['cameras']['legacy_entrance']['enabled'])
        with self.repo.connect() as c:
            payload = c.execute('SELECT payload FROM frigate_import_handoffs').fetchone()[0]
        self.assertNotIn(b'synthetic-password', payload)
        self.assertNotIn(b'rtsp://', payload)
        import threading
        from camadmiral import app
        with patch.object(app, '_repository', return_value=self.repo), \
             patch.object(app, 'FRIGATE_LOCK', threading.Lock()):
            response = app.remove_frigate_target(self.target.target_id, 'remove-frigate-target')
        self.assertEqual(409, response.status_code)
        self.assertIsNotNone(self.repo.frigate_target(self.target.target_id))
        store.interrupt_jobs()
        recover_handoffs(self.repo, lambda: True, lambda target: self.client)
        self.assertEqual(self.camera, self.client.raw_config()['cameras']['legacy_entrance'])
        self.assertEqual({}, store.links(self.target.target_id))
        self.assertEqual('interrupted_import', store.job(self.target.target_id, job['id'])['cameras'][0]['reason'])
        self.assertEqual('restored', public_job(store.job(self.target.target_id, job['id']))['cameras'][0]['configuration_status'])
        self.assertEqual([], self.repo.consumer_inventory())

    def test_recovery_keeps_conflicting_operator_edits_and_recovery_journal(self):
        class ProcessLost(BaseException): pass
        job = self.preview()
        with self.assertRaises(ProcessLost): self.capped(job, after_release=ProcessLost())
        self.client.current_raw_config['cameras']['legacy_entrance']['detect']['fps'] = 9
        recover_handoffs(self.repo, lambda: True, lambda target: self.client)
        self.assertEqual(9, self.client.raw_config()['cameras']['legacy_entrance']['detect']['fps'])
        self.assertTrue(ImportStore(self.repo).links(self.target.target_id))
        self.assertEqual('rollback_required', ImportStore(self.repo).job(self.target.target_id, job['id'])['cameras'][0]['reason'])
        self.assertEqual('restore_required', public_job(ImportStore(self.repo).job(self.target.target_id, job['id']))['cameras'][0]['configuration_status'])

    def test_startup_restoration_waits_for_target_api_to_return(self):
        class ProcessLost(BaseException): pass
        job = self.preview()
        with self.assertRaises(ProcessLost): self.capped(job, after_release=ProcessLost())
        raw_config = self.client.raw_config
        attempts = []
        def temporarily_unavailable():
            attempts.append(True)
            if len(attempts) == 1:
                raise FrigateApiError('target_unavailable')
            return raw_config()
        with patch.object(self.client, 'raw_config', side_effect=temporarily_unavailable), \
             patch('camadmiral.frigate_import.time.sleep'):
            recover_handoffs(self.repo, lambda: True, lambda target: self.client)
        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual(self.camera, raw_config()['cameras']['legacy_entrance'])
        self.assertEqual({}, ImportStore(self.repo).links(self.target.target_id))
        self.assertEqual('interrupted_import', ImportStore(self.repo).job(self.target.target_id, job['id'])['cameras'][0]['reason'])

    def test_handoff_restore_refuses_to_disable_relay_used_by_another_integration(self):
        job = self.capped(self.preview())
        cid = job['cameras'][0]['camera_uuid']
        self.repo.save_frigate_target('another-target', 'Another target', 'http://other:5000')
        self.repo.select_frigate_camera('another-target', cid)
        writes = len(self.client.config_writes)
        with self.assertRaises(FrigateApiError) as error:
            restore(self.repo, self.target, 'legacy_entrance', self.client, lambda: True)
        self.assertEqual('restore_media_in_use', error.exception.code)
        self.assertTrue(self.repo.consumer_inventory()[0]['enabled'])
        self.assertEqual(writes, len(self.client.config_writes))

    def test_record_only_failure_never_commits_on_positive_detection_fps(self):
        with patch('camadmiral.frigate_import._wait_record_reader', side_effect=FrigateApiError('recording_verification_failed')):
            job = self.capped(self.preview())
        self.assertEqual('recording_verification_failed', job['cameras'][0]['reason'])
        self.assertEqual([], self.repo.selected_frigate_camera_uuids(self.target.target_id))
        self.assertEqual(self.camera, self.client.raw_config()['cameras']['legacy_entrance'])
        self.assertFalse(self.repo.consumer_inventory()[0]['enabled'])

    def test_recovery_finds_new_adoption_after_lost_acknowledgement(self):
        class ProcessLost(BaseException): pass
        adopt = self.repo.adopt
        def lost(*args, **kwargs):
            adopt(*args, **kwargs)
            raise ProcessLost()
        with patch.object(self.repo, 'adopt', side_effect=lost), self.assertRaises(ProcessLost):
            self.capped(self.preview())
        import threading
        from camadmiral import app
        camera_uuid = self.repo.consumer_inventory()[0]['camera_uuid']
        with patch.object(app, '_repository', return_value=self.repo), \
             patch.object(app, '_candidate_is_currently_online', return_value=False), \
             patch.object(app, 'FRIGATE_LOCK', threading.Lock()):
            response = app.unadopt_camera(camera_uuid, 'unadopt-camera')
        self.assertEqual(409, response.status_code)
        self.assertEqual('import_restore_required', json.loads(response.body)['status'])
        recover_handoffs(self.repo, lambda: True, lambda target: self.client)
        self.assertEqual(self.camera, self.client.raw_config()['cameras']['legacy_entrance'])
        self.assertFalse(self.repo.consumer_inventory()[0]['enabled'])
        self.assertEqual({}, ImportStore(self.repo).links(self.target.target_id))

    def test_recovery_never_writes_to_a_changed_target(self):
        job = self.capped(self.preview())
        other = FrigateTarget(self.target.target_id, 'Changed target', 'http://different:5000')
        writes = len(self.client.config_writes)
        with self.assertRaises(FrigateApiError) as error:
            restore(self.repo, other, 'legacy_entrance', self.client, lambda: True)
        self.assertEqual('rollback_target_changed', error.exception.code)
        self.assertEqual(writes, len(self.client.config_writes))

    def execute(self, job, selected=None, keep=()):
        with patch('camadmiral.frigate_import.probe_source', return_value=ProbeResult('ready', 1, 'h264', width=640, height=360, fps=10)), \
             patch('camadmiral.frigate_import._target_host', return_value='192.0.2.20'), \
             patch('camadmiral.frigate_import.snapshot_frame', return_value=b'\xff\xd8\xffsynthetic\xff\xd9'):
            return execute(self.repo, self.target, job['id'], selected or ['legacy_entrance'], list(keep), None,
                           lambda: True, self.client)

    def legacy_masked_job(self):
        job = self.preview()
        for source in job['cameras'][0]['sources'].values():
            source.update(username='*', password='*')
        job['status'] = 'completed'
        job['cameras'][0].update(status='error', reason='source_auth_failed', configuration_status='unchanged')
        ImportStore(self.repo).save_job(self.target.target_id, job)
        return job

    def test_retry_recovers_credentials_in_a_persisted_masked_import(self):
        job = self.legacy_masked_job()
        result = self.execute(job)
        self.assertEqual('applied', result['cameras'][0]['status'])
        candidate = 'frigate-import:' + self.target.target_id + ':legacy_entrance'
        self.assertEqual(('operator', 'synthetic-password'), self.repo.credentials_for_candidate(candidate))

    def test_retry_does_not_retarget_a_persisted_masked_import(self):
        job = self.legacy_masked_job()
        self.client.current_raw_paths['cameras']['legacy_entrance']['ffmpeg']['inputs'][0]['path'] = 'rtsp://operator:synthetic-password@192.0.2.99/changed'
        result = self.execute(job)
        self.assertEqual('camera_changed_refresh_preview', result['cameras'][0]['reason'])
        self.assertEqual([], self.client.config_writes)
        self.assertEqual([], self.repo.consumer_inventory())

    def test_preview_uses_unmasked_admin_paths_for_authenticated_direct_inputs(self):
        camera = self.client.current_config['cameras']['legacy_entrance']
        camera['ffmpeg']['inputs'][0]['path'] = 'rtsp://*:*@192.0.2.10/main'
        before = copy.deepcopy(self.client.current_config)
        job = self.preview()
        source = next(iter(job['cameras'][0]['sources'].values()))
        self.assertEqual(('operator', 'synthetic-password'), (source['username'], source['password']))
        self.assertEqual('rtsp://192.0.2.10/main', source['uri'])
        self.assertEqual(before, self.client.current_config)
        self.assertEqual([], self.client.config_writes)
        self.assertNotIn('synthetic-password', json.dumps(public_job(job)))

    def test_preview_uses_resolved_admin_paths_when_saved_yaml_has_variables(self):
        saved = self.client.current_raw_config['cameras']['legacy_entrance']
        saved['ffmpeg']['inputs'][0]['path'] = 'rtsp://{FRIGATE_SYNTHETIC_USER}:{FRIGATE_SYNTHETIC_PASSWORD}@192.0.2.10/main'
        self.client.current_config['cameras']['legacy_entrance']['ffmpeg']['inputs'][0]['path'] = 'rtsp://*:*@192.0.2.10/main'
        job = self.preview()
        source = next(iter(job['cameras'][0]['sources'].values()))
        self.assertEqual(('operator', 'synthetic-password'), (source['username'], source['password']))
        self.assertEqual(saved, job['cameras'][0]['original'])

    def test_preview_rejects_missing_admin_input_paths(self):
        self.client.current_raw_paths['cameras'].pop('legacy_entrance')
        self.client.current_raw_config['cameras']['legacy_entrance'].pop('ffmpeg')
        job = self.preview()
        self.assertFalse(job['cameras'][0]['can_import'])
        self.assertEqual('unsupported_source', job['cameras'][0]['reason'])
        self.assertEqual([], self.client.config_writes)
        self.assertEqual([], self.repo.consumer_inventory())

    def test_preview_uses_trusted_saved_paths_before_runtime_restart(self):
        self.client.current_raw_paths['cameras'].pop('legacy_entrance')
        self.client.current_config['cameras']['legacy_entrance']['ffmpeg']['inputs'][0]['path'] = 'rtsp://*:*@192.0.2.10/main'
        job = self.preview()
        self.assertTrue(job['cameras'][0]['can_import'])
        source = next(iter(job['cameras'][0]['sources'].values()))
        self.assertEqual(('operator', 'synthetic-password'), (source['username'], source['password']))
        self.assertEqual([], self.client.config_writes)

    def test_preview_rejects_unresolved_saved_variables_without_runtime_paths(self):
        self.client.current_raw_paths['cameras'].pop('legacy_entrance')
        self.client.current_raw_config['cameras']['legacy_entrance']['ffmpeg']['inputs'][0]['path'] = 'rtsp://{FRIGATE_SYNTHETIC_USER}:{FRIGATE_SYNTHETIC_PASSWORD}@192.0.2.10/main'
        self.client.current_config['cameras']['legacy_entrance']['ffmpeg']['inputs'][0]['path'] = 'rtsp://*:*@192.0.2.10/main'
        job = self.preview()
        self.assertFalse(job['cameras'][0]['can_import'])
        self.assertEqual('unresolved_secret', job['cameras'][0]['reason'])
        self.assertEqual([], self.client.config_writes)
        self.assertEqual([], self.repo.consumer_inventory())

    def test_preview_redacts_credentials_and_does_not_adopt_or_write(self):
        job = self.preview()
        self.assertEqual('new', job['cameras'][0]['category'])
        text = json.dumps(public_job(job))
        self.assertNotIn('synthetic-password', text)
        self.assertNotIn('synthetic-onvif-password', text)
        self.assertNotIn('rtsp://', text)
        self.assertEqual([], self.repo.consumer_inventory())
        self.assertEqual([], self.client.config_writes)
        with self.repo.connect() as c:
            blob = c.execute('SELECT payload FROM frigate_import_jobs').fetchone()[0]
        self.assertNotIn(b'synthetic-password', blob)
        self.assertNotIn(b'synthetic-onvif-password', blob)

    def test_progress_retains_selection_and_publishes_safe_stages(self):
        job = self.preview()
        snapshots = []
        save = ImportStore.save_job

        def observe(store, target_id, current):
            snapshots.append(copy.deepcopy(public_job(current)))
            return save(store, target_id, current)

        with patch.object(ImportStore, 'save_job', autospec=True, side_effect=observe):
            result = self.execute(job)
        stages = {v['cameras'][0]['phase'] for v in snapshots}
        self.assertTrue({'checking_configuration', 'validating_source', 'adding_camera',
                         'preparing_relay', 'validating_relay', 'saving_frigate',
                         'waiting_for_frigate'} <= stages)
        for snapshot in snapshots:
            self.assertEqual(['legacy_entrance'], snapshot['selected'])
            self.assertEqual([], snapshot['keep_existing'])
            if snapshot['cameras'][0]['phase']:
                self.assertEqual('running', snapshot['cameras'][0]['status'])
            text = json.dumps(snapshot)
            self.assertNotIn('synthetic-password', text)
            self.assertNotIn('rtsp://', text)
        self.assertIsNone(result['cameras'][0]['phase'])

    def test_retry_queues_failed_selection_without_resetting_successes(self):
        job = self.preview()
        good = copy.deepcopy(job['cameras'][0])
        good.update(key='completed_camera', status='applied')
        job['cameras'].append(good)
        job['cameras'][0].update(status='error', reason='source_unavailable', configuration_status='restored')
        start_job(job, ['legacy_entrance'], ['legacy_entrance'])
        result = public_job(job)
        self.assertEqual(['legacy_entrance'], result['selected'])
        self.assertEqual(['legacy_entrance'], result['keep_existing'])
        self.assertEqual('queued', result['cameras'][0]['status'])
        self.assertIsNone(result['cameras'][0]['reason'])
        self.assertIsNone(result['cameras'][0]['configuration_status'])
        self.assertEqual('applied', result['cameras'][1]['status'])

    def test_shared_endpoint_channels_are_separate_and_relay_cycles_rejected(self):
        second = copy.deepcopy(self.camera)
        second['ffmpeg']['inputs'][0]['path'] = 'rtsp://operator:synthetic-password@192.0.2.10/other-channel'
        self.client.seed('other_channel', second)
        job = self.preview()
        self.assertEqual(['new', 'new'], [r['category'] for r in job['cameras']])
        self.assertNotEqual(job['cameras'][0]['sources'], job['cameras'][1]['sources'])
        with self.assertRaises(FrigateApiError):
            resolve_source('rtsp://localhost:8554/a', {'a': ['rtsp://localhost:8554/a']})

    def test_encoded_credentials_and_ffmpeg_live_wrapper(self):
        src = resolve_source('ffmpeg:rtsp://us%40er:synthetic%3Asecret@192.0.2.1/main#audio=opus', {})
        self.assertEqual('us@er', src['username'])
        self.assertEqual('synthetic:secret', src['password'])
        self.assertNotIn('@', src['uri'])
        self.assertEqual('#audio=opus', src['wrapper'])
        data = inspect_camera('camera', {}, {'ffmpeg': {'inputs': [
            {'path': 'rtsp://localhost:8554/a', 'roles': ['record', 'detect']}]},
            'live': {'streams': {'View': 'a'}}}, {'a': ['rtsp://192.0.2.1/main']})
        self.assertEqual(1, len(data['sources']))

    def test_takeover_preserves_key_name_tuning_and_repeat_is_managed(self):
        job = self.execute(self.preview())
        self.assertEqual('applied', job['cameras'][0]['status'])
        actual = self.client.raw_config()['cameras']['legacy_entrance']
        for key in self.camera:
            if key != 'ffmpeg':
                self.assertEqual(self.camera[key], actual[key], key)
        self.assertEqual({'legacy_entrance'}, set(self.client.raw_config()['cameras']))
        self.assertEqual(1, len(self.repo.consumer_inventory()))
        self.assertEqual('managed', self.preview()['cameras'][0]['category'])
        self.execute(job)
        self.assertEqual(1, len(self.repo.consumer_inventory()))
        keys, aliases = desired_resource_keys(self.repo, self.target.target_id)
        self.assertEqual({'legacy_entrance'}, keys)
        self.assertEqual(2, len(aliases))

    def test_disabled_and_unresolved_sources_remain_unchanged(self):
        disabled = copy.deepcopy(self.camera)
        disabled['enabled'] = False
        self.client.seed('legacy_entrance', disabled)
        job = self.execute(self.preview())
        self.assertEqual('pending', job['cameras'][0]['status'])
        self.assertEqual([], self.repo.consumer_inventory())
        self.assertEqual(disabled, self.client.raw_config()['cameras']['legacy_entrance'])
        with self.assertRaises(FrigateApiError):
            resolve_source('rtsp://{FRIGATE_USER}:{FRIGATE_PASSWORD}@192.0.2.1/main', {})

    def test_failed_validation_and_stale_preview_never_switch_sources(self):
        job = self.preview()
        with patch('camadmiral.frigate_import.probe_source', return_value=ProbeResult('auth_failed', 1)):
            result = execute(self.repo, self.target, job['id'], ['legacy_entrance'], [], None, lambda: True, self.client)
        self.assertEqual('source_auth_failed', result['cameras'][0]['reason'])
        self.assertEqual([], self.client.config_writes)
        self.assertEqual([], self.repo.consumer_inventory())
        job = self.preview()
        self.client.current_raw_config['cameras']['legacy_entrance']['detect']['fps'] = 7
        result = self.execute(job)
        self.assertEqual('configuration_changed_refresh_preview', result['cameras'][0]['reason'])
        self.assertEqual([], self.client.config_writes)

    def test_restore_preserves_original_entry_and_releases_binding(self):
        self.execute(self.preview())
        restore(self.repo, self.target, 'legacy_entrance', self.client)
        self.assertEqual(self.camera, self.client.raw_config()['cameras']['legacy_entrance'])
        self.assertEqual({}, ImportStore(self.repo).links(self.target.target_id))
        self.assertEqual([], self.repo.selected_frigate_camera_uuids(self.target.target_id))
        self.assertEqual(1, len(self.repo.consumer_inventory()), 'Rollback must not delete adopted camera data')

    def test_batch_and_restore_confirm_capture_after_lost_restart_reply(self):
        second = copy.deepcopy(self.camera)
        second['ffmpeg']['inputs'][0]['path'] = 'rtsp://operator:synthetic-password@192.0.2.11/main'
        self.client.seed('legacy_loading', second)
        restart = self.client.restart

        def lost_reply():
            restart()
            raise FrigateApiError('target_unavailable')

        with patch.object(self.client, 'restart', side_effect=lost_reply):
            job = self.execute(self.preview(), ['legacy_entrance', 'legacy_loading'])
            self.assertEqual(['applied', 'applied'], [r['status'] for r in job['cameras']])
            self.assertEqual(2, len(self.repo.selected_frigate_camera_uuids(self.target.target_id)))
            self.assertEqual({'legacy_entrance', 'legacy_loading'}, set(self.client.raw_config()['cameras']))
            restore(self.repo, self.target, 'legacy_entrance', self.client)
        self.assertEqual(self.camera, self.client.raw_config()['cameras']['legacy_entrance'])
        self.assertEqual({'legacy_loading'}, set(ImportStore(self.repo).links(self.target.target_id)))
        self.assertEqual(2, len(self.repo.consumer_inventory()))

    def test_explicit_restart_rejection_does_not_commit_ownership(self):
        with patch.object(self.client, 'restart', side_effect=FrigateApiError('restart_rejected')):
            job = self.execute(self.preview())
        self.assertEqual('error', job['cameras'][0]['status'])
        self.assertEqual([], self.repo.selected_frigate_camera_uuids(self.target.target_id))
        self.assertNotEqual('active', next(iter(ImportStore(self.repo).links(self.target.target_id).values()))['state'])
        self.assertEqual(self.camera, self.client.raw_config()['cameras']['legacy_entrance'])

    def test_incomplete_http_restart_reply_is_a_transport_interruption(self):
        client = FrigateClient(self.target)
        with patch.object(client._opener, 'open') as opened:
            response = opened.return_value.__enter__.return_value
            response.headers = {}
            response.read.side_effect = http.client.IncompleteRead(b'{"success":', 6)
            with self.assertRaises(FrigateApiError) as error:
                client.restart()
        self.assertEqual('target_unavailable', error.exception.code)

    def test_empty_restart_reply_requires_capture_before_committing(self):
        restart = self.client.restart

        def empty_reply():
            restart()
            raise FrigateApiError('invalid_response')

        with patch.object(self.client, 'restart', side_effect=empty_reply):
            job = self.execute(self.preview())
            self.assertEqual('applied', job['cameras'][0]['status'])
            restore(self.repo, self.target, 'legacy_entrance', self.client)
        self.assertEqual(self.camera, self.client.raw_config()['cameras']['legacy_entrance'])

    def test_lost_restart_reply_still_requires_verified_capture(self):
        restart = self.client.restart

        def lost_reply():
            restart()
            raise FrigateApiError('target_unavailable')

        with patch.object(self.client, 'restart', side_effect=lost_reply), \
             patch('camadmiral.frigate_import._wait_capture', side_effect=[
                 FrigateApiError('capture_verification_failed'), None]):
            job = self.execute(self.preview())
        self.assertEqual('capture_verification_failed', job['cameras'][0]['reason'])
        self.assertEqual(self.camera, self.client.raw_config()['cameras']['legacy_entrance'])
        self.assertEqual({}, ImportStore(self.repo).links(self.target.target_id))
        self.assertEqual([], self.repo.selected_frigate_camera_uuids(self.target.target_id))

    def test_retry_after_interrupted_save_restores_then_reuses_camera(self):
        job = self.execute(self.preview())
        store = ImportStore(self.repo)
        row = store.links(self.target.target_id)['legacy_entrance']
        row.update(state='applying', status='error')
        store.save_link(self.target.target_id, 'legacy_entrance', row)
        job['cameras'][0].update(status='error')
        store.save_job(self.target.target_id, job)
        result = self.execute(job)
        self.assertEqual('applied', result['cameras'][0]['status'])
        self.assertEqual(1, len(self.repo.consumer_inventory()))

    def test_sync_preserves_operator_edits_and_reports_source_conflicts(self):
        job = self.execute(self.preview())
        self.client.current_raw_config['cameras']['legacy_entrance']['detect']['fps'] = 9
        self.client.current_raw_config['cameras']['legacy_entrance']['friendly_name'] = 'Operator adjusted name'
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: '192.0.2.21')
        actual = self.client.raw_config()['cameras']['legacy_entrance']
        self.assertEqual(9, actual['detect']['fps'])
        self.assertEqual('Operator adjusted name', actual['friendly_name'])
        self.assertNotIn('camadmiral_' + job['cameras'][0]['camera_uuid'].replace('-', '_'), self.client.raw_config()['cameras'])

    def test_known_offline_roles_defer_refresh_until_video_returns(self):
        job = self.execute(self.preview())
        uid = job["cameras"][0]["camera_uuid"]
        self.repo.record_address_change(uid, "192.0.2.10", "192.0.2.11", "onvif_identity")
        with self.repo.connect() as c:
            c.execute("UPDATE managed_streams SET health_status='offline' WHERE camera_uuid=?", (uid,))
            c.commit()
        self.client.operations.clear()
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual([], self.client.operations)
        self.assertNotIn("relay_address_revision", ImportStore(self.repo).links(self.target.target_id)["legacy_entrance"])
        with self.repo.connect() as c:
            c.execute("UPDATE managed_streams SET health_status='healthy',probe_status='ready' WHERE camera_uuid=?", (uid,))
            c.commit()
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(self.repo.latest_camera_address_revision(uid), ImportStore(self.repo).links(self.target.target_id)["legacy_entrance"]["relay_address_revision"])

    def test_failed_targeted_pause_restarts_target_once_and_preserves_configuration(self):
        job = self.execute(self.preview())
        uid = job["cameras"][0]["camera_uuid"]
        self.repo.record_address_change(uid, "192.0.2.10", "192.0.2.11", "onvif_identity")
        before = self.client.raw_config()
        restarts = self.client.restart_calls
        enable = self.client.set_runtime_camera_enabled
        def stalled(key, enabled):
            enable(key, enabled)
            if not enabled:
                raise FrigateApiError("target_unavailable")
        with patch.object(self.client, "set_runtime_camera_enabled", side_effect=stalled):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(restarts + 1, self.client.restart_calls)
        self.assertEqual(before, self.client.raw_config())
        row = ImportStore(self.repo).links(self.target.target_id)["legacy_entrance"]
        self.assertEqual(self.repo.latest_camera_address_revision(uid), row["relay_address_revision"])
        self.assertNotIn("capture_resume_pending", row)
        self.assertNotIn("target_restart", row)
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(restarts + 1, self.client.restart_calls)

    def test_restart_fallback_accepts_frigate_default_detection_disabled(self):
        self.execute(self.preview())
        self.assertNotIn("enabled", self.client.raw_config()["cameras"]["legacy_entrance"]["detect"])
        self.client.current_config["cameras"]["legacy_entrance"]["detect"]["enabled"] = False
        row = ImportStore(self.repo).links(self.target.target_id)["legacy_entrance"]
        self.repo.record_address_change(row["camera_uuid"], "192.0.2.10", "192.0.2.11", "onvif_identity")
        before, restarts = self.client.raw_config(), self.client.restart_calls
        with patch.object(self.client, "set_runtime_camera_enabled", side_effect=FrigateApiError("target_unavailable")):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(restarts + 1, self.client.restart_calls)
        self.assertEqual(before, self.client.raw_config())
        row = ImportStore(self.repo).links(self.target.target_id)["legacy_entrance"]
        self.assertNotIn("capture_resume_pending", row)
        self.assertNotIn("target_restart", row)

    def test_restart_fallback_preserves_runtime_detection_enabled_above_default(self):
        self.execute(self.preview())
        self.client.current_config["cameras"]["legacy_entrance"]["detect"]["enabled"] = True
        row = ImportStore(self.repo).links(self.target.target_id)["legacy_entrance"]
        self.repo.record_address_change(row["camera_uuid"], "192.0.2.10", "192.0.2.11", "onvif_identity")
        before, restarts = self.client.raw_config(), self.client.restart_calls
        with patch.object(self.client, "set_runtime_camera_enabled", side_effect=FrigateApiError("target_unavailable")):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(restarts, self.client.restart_calls)
        self.assertTrue(self.client.config()["cameras"]["legacy_entrance"]["detect"]["enabled"])
        self.assertEqual(before, self.client.raw_config())

    def test_pending_resume_uses_restart_fallback_if_runtime_control_remains_unavailable(self):
        self.execute(self.preview())
        store = ImportStore(self.repo)
        row = store.links(self.target.target_id)["legacy_entrance"]
        row["capture_resume_pending"] = True
        store.save_link(self.target.target_id, row["key"], row)
        self.client.current_config["cameras"]["legacy_entrance"]["enabled"] = False
        before, restarts = self.client.raw_config(), self.client.restart_calls
        with patch.object(self.client, "set_runtime_camera_enabled", side_effect=FrigateApiError("target_unavailable")):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(restarts + 1, self.client.restart_calls)
        self.assertNotIn("capture_resume_pending", store.links(self.target.target_id)["legacy_entrance"])
        self.assertEqual(before, self.client.raw_config())

    def test_uncertain_restart_is_verified_without_dispatching_another_restart(self):
        job = self.execute(self.preview())
        uid = job["cameras"][0]["camera_uuid"]
        self.repo.record_address_change(uid, "192.0.2.10", "192.0.2.11", "onvif_identity")
        enable = self.client.set_runtime_camera_enabled
        def stalled(key, enabled):
            enable(key, enabled)
            if not enabled:
                raise FrigateApiError("target_unavailable")
        with patch.object(self.client, "set_runtime_camera_enabled", side_effect=stalled), \
             patch.object(self.client, "restart", side_effect=FrigateApiError("target_unavailable")) as restart, \
             patch("camadmiral.frigate_import.READER_RESTART_VERIFY_TIMEOUT_SECONDS", 0):
            for _ in range(2):
                sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
            self.assertEqual(1, restart.call_count)
        store = ImportStore(self.repo)
        self.assertIn("target_restart", store.links(self.target.target_id)["legacy_entrance"])
        self.client.restart()
        restarts = self.client.restart_calls
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(restarts, self.client.restart_calls)
        self.assertNotIn("target_restart", store.links(self.target.target_id)["legacy_entrance"])
        self.assertEqual(self.repo.latest_camera_address_revision(uid), store.links(self.target.target_id)["legacy_entrance"]["relay_address_revision"])

    def test_global_restart_refreshes_both_owned_channels_once(self):
        second = copy.deepcopy(self.camera)
        second["ffmpeg"]["inputs"][0]["path"] = "rtsp://operator:synthetic-password@192.0.2.12/main"
        second["onvif"]["host"] = "192.0.2.12"
        self.client.seed("second_camera", second)
        job = self.execute(self.preview(), selected=["legacy_entrance", "second_camera"])
        for row in job["cameras"]:
            self.repo.record_address_change(row["camera_uuid"], "192.0.2.10", "192.0.2.11", "onvif_identity")
        before = self.client.raw_config()
        restarts = self.client.restart_calls
        with patch.object(self.client, "set_runtime_camera_enabled", side_effect=FrigateApiError("target_unavailable")):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(restarts + 1, self.client.restart_calls)
        links = ImportStore(self.repo).links(self.target.target_id)
        self.assertEqual(2, len(links))
        for row in links.values():
            self.assertEqual(self.repo.latest_camera_address_revision(row["camera_uuid"]), row["relay_address_revision"])
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(restarts + 1, self.client.restart_calls)
        self.assertEqual(before, self.client.raw_config())

    def test_restart_fallback_does_not_wait_for_or_commit_known_offline_peers(self):
        second = copy.deepcopy(self.camera)
        second["ffmpeg"]["inputs"][0]["path"] = "rtsp://operator:synthetic-password@192.0.2.12/main"
        second["onvif"]["host"] = "192.0.2.12"
        self.client.seed("second_camera", second)
        job = self.execute(self.preview(), selected=["legacy_entrance", "second_camera"])
        links = ImportStore(self.repo).links(self.target.target_id)
        offline = links["second_camera"]["camera_uuid"]
        with self.repo.connect() as c:
            c.execute("UPDATE managed_streams SET health_status='offline' WHERE camera_uuid=?", (offline,))
            c.commit()
        for row in links.values():
            self.repo.record_address_change(row["camera_uuid"], "192.0.2.10", "192.0.2.11", "onvif_identity")
        def reader(client, row):
            if row["camera_uuid"] == offline:
                raise FrigateApiError("recording_verification_failed")
        with patch.object(self.client, "set_runtime_camera_enabled", side_effect=FrigateApiError("target_unavailable")), \
             patch("camadmiral.frigate_import._wait_record_reader", side_effect=reader):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        links = ImportStore(self.repo).links(self.target.target_id)
        self.assertNotIn("target_restart", links["legacy_entrance"])
        self.assertEqual(self.repo.latest_camera_address_revision(links["legacy_entrance"]["camera_uuid"]), links["legacy_entrance"]["relay_address_revision"])
        self.assertNotIn("relay_address_revision", links["second_camera"])
        restarts = self.client.restart_calls
        with self.repo.connect() as c:
            c.execute("UPDATE managed_streams SET health_status='healthy',probe_status='ready' WHERE camera_uuid=?", (offline,))
            c.commit()
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(restarts, self.client.restart_calls)
        row = ImportStore(self.repo).links(self.target.target_id)["second_camera"]
        self.assertEqual(self.repo.latest_camera_address_revision(offline), row["relay_address_revision"])

    def test_restart_fallback_preserves_another_camera_runtime_pause(self):
        job = self.execute(self.preview())
        uid = job["cameras"][0]["camera_uuid"]
        self.repo.record_address_change(uid, "192.0.2.10", "192.0.2.11", "onvif_identity")
        self.client.seed("operator_camera", {"enabled": True})
        self.client.current_config["cameras"]["operator_camera"]["enabled"] = False
        restarts = self.client.restart_calls
        enable = self.client.set_runtime_camera_enabled
        def stalled(key, enabled):
            enable(key, enabled)
            if not enabled:
                raise FrigateApiError("target_unavailable")
        with patch.object(self.client, "set_runtime_camera_enabled", side_effect=stalled):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(restarts, self.client.restart_calls)
        self.assertFalse(self.client.config()["cameras"]["operator_camera"]["enabled"])
        self.assertTrue(self.client.config()["cameras"]["legacy_entrance"]["enabled"])

    def test_reader_refresh_reconnects_capture_without_persisted_configuration_changes(self):
        job = self.execute(self.preview())
        uid = job["cameras"][0]["camera_uuid"]
        self.repo.record_address_change(uid, "192.0.2.10", "192.0.2.11", "onvif_identity")
        before = self.client.raw_config()
        self.client.operations.clear()
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(("set_runtime_camera_enabled", "legacy_entrance", False), self.client.operations[0])
        self.assertEqual(("set_runtime_camera_enabled", "legacy_entrance", True), self.client.operations[-1])
        self.assertEqual(before, self.client.raw_config())
        self.assertTrue(self.client.config()["cameras"]["legacy_entrance"]["enabled"])

    def test_reader_refresh_resumes_capture_after_ambiguous_pause(self):
        job = self.execute(self.preview())
        uid = job["cameras"][0]["camera_uuid"]
        self.repo.record_address_change(uid, "192.0.2.10", "192.0.2.11", "onvif_identity")
        enable = self.client.set_runtime_camera_enabled
        def ambiguous(key, enabled):
            enable(key, enabled)
            if not enabled:
                raise FrigateApiError("target_unavailable")
        self.client.operations.clear()
        with patch.object(self.client, "set_runtime_camera_enabled", side_effect=ambiguous):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertTrue(self.client.config()["cameras"]["legacy_entrance"]["enabled"])
        row = ImportStore(self.repo).links(self.target.target_id)["legacy_entrance"]
        self.assertNotIn("capture_resume_pending", row)
        self.assertEqual(self.repo.latest_camera_address_revision(uid), row["relay_address_revision"])

    def test_reader_refresh_recovers_pending_resume_before_reconnecting_again(self):
        job = self.execute(self.preview())
        uid = job["cameras"][0]["camera_uuid"]
        self.repo.record_address_change(uid, "192.0.2.10", "192.0.2.11", "onvif_identity")
        self.client.seed("operator_camera", {"enabled": True})
        self.client.current_config["cameras"]["operator_camera"]["enabled"] = False
        enable = self.client.set_runtime_camera_enabled
        def resume_unavailable(key, enabled):
            if enabled:
                raise FrigateApiError("target_unavailable")
            enable(key, enabled)
        with patch.object(self.client, "set_runtime_camera_enabled", side_effect=resume_unavailable):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        store = ImportStore(self.repo)
        self.assertTrue(store.links(self.target.target_id)["legacy_entrance"]["capture_resume_pending"])
        self.client.operations.clear()
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual([("set_runtime_camera_enabled", "legacy_entrance", True)], self.client.operations)
        self.assertNotIn("capture_resume_pending", store.links(self.target.target_id)["legacy_entrance"])
        self.assertNotIn("relay_address_revision", store.links(self.target.target_id)["legacy_entrance"])
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(self.repo.latest_camera_address_revision(uid), store.links(self.target.target_id)["legacy_entrance"]["relay_address_revision"])

    def test_reader_refresh_preserves_operator_runtime_pause(self):
        job = self.execute(self.preview())
        uid = job["cameras"][0]["camera_uuid"]
        self.repo.record_address_change(uid, "192.0.2.10", "192.0.2.11", "onvif_identity")
        self.client.current_config["cameras"]["legacy_entrance"]["enabled"] = False
        self.client.operations.clear()
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual([], self.client.operations)

    def test_shared_relay_restart_refreshes_unchanged_peer_without_config_writes(self):
        second = copy.deepcopy(self.camera)
        second["ffmpeg"]["inputs"][0]["path"] = "rtsp://operator:synthetic-password@192.0.2.12/main"
        second["onvif"]["host"] = "192.0.2.12"
        self.client.seed("second_camera", second)
        self.execute(self.preview(), selected=["legacy_entrance", "second_camera"])
        links = ImportStore(self.repo).links(self.target.target_id)
        self.repo.record_address_change(links["legacy_entrance"]["camera_uuid"],
                                        "192.0.2.10", "192.0.2.11", "onvif_identity")
        # Replacing one source restarts the shared relay, with alerts disabled.
        self.repo.enqueue_relay_restart_notification(reason="camera_address_recovery", camera_count=1)
        before = self.client.raw_config()
        self.client.operations.clear()
        self.client.runtime_writes.clear()
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(sorted(alias for row in links.values() for alias in row["managed_streams"]),
                         sorted(alias for alias, _ in self.client.runtime_writes))
        self.assertEqual({"legacy_entrance", "second_camera"},
                         {op[1] for op in self.client.operations if op[0] == "set_runtime_camera_enabled"})
        updated = ImportStore(self.repo).links(self.target.target_id)
        self.assertEqual(1, len({row["relay_runtime_revision"] for row in updated.values()}))
        self.assertNotEqual(links["second_camera"]["relay_runtime_revision"], updated["second_camera"]["relay_runtime_revision"])
        self.assertEqual(before, self.client.raw_config())
        operations = list(self.client.operations)
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(operations, self.client.operations)

    def test_control_restart_refreshes_imported_readers_once_without_address_change(self):
        job = self.execute(self.preview())
        row = ImportStore(self.repo).links(self.target.target_id)["legacy_entrance"]
        aliases = sorted(row["managed_streams"])
        before = self.client.raw_config()
        self.client.operations.clear()
        self.client.runtime_writes.clear()
        with patch("camadmiral.frigate_import.RELAY_RUNTIME_REVISION", "synthetic-next-runtime", create=True):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
            self.assertEqual(aliases, [alias for alias, _ in self.client.runtime_writes])
            operations = list(self.client.operations)
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
            self.assertEqual(operations, self.client.operations)
        self.assertEqual(before, self.client.raw_config())
        self.assertTrue(all(op[0] in {"delete_runtime_stream", "set_runtime_stream", "set_runtime_camera_enabled"} for op in operations))

    def test_committed_address_change_refreshes_only_imported_readers_without_config_writes(self):
        job = self.execute(self.preview())
        camera_uuid = job["cameras"][0]["camera_uuid"]
        row = ImportStore(self.repo).links(self.target.target_id)["legacy_entrance"]
        aliases = sorted(row["managed_streams"])
        self.client.current_raw_config["go2rtc"]["streams"]["operator_owned"] = ["rtsp://192.0.2.44/live"]
        before = self.client.raw_config()
        self.client.operations.clear()
        self.client.runtime_writes.clear()
        self.repo.record_address_change(camera_uuid, "192.0.2.10", "192.0.2.11", "onvif_identity")

        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")

        self.assertEqual(aliases, [alias for alias, _ in self.client.runtime_writes])
        self.assertEqual({a: row["managed_streams"][a][0] for a in aliases}, dict(self.client.runtime_writes))
        self.assertEqual(before, self.client.raw_config())
        self.assertTrue(all(op[0] in {"delete_runtime_stream", "set_runtime_stream", "set_runtime_camera_enabled"} for op in self.client.operations))
        operations = list(self.client.operations)
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(operations, self.client.operations, "Unchanged address must not reconnect readers repeatedly")

    def test_reader_refresh_recreates_owned_aliases_and_preserves_configuration(self):
        self.execute(self.preview())
        store = ImportStore(self.repo)
        row = store.links(self.target.target_id)["legacy_entrance"]
        self.repo.record_address_change(row["camera_uuid"], "192.0.2.10", "192.0.2.11", "onvif_identity")
        before = self.client.raw_config()
        self.client.operations.clear()
        self.client.runtime_writes.clear()
        with patch.object(self.client, "delete_runtime_stream", wraps=self.client.delete_runtime_stream) as delete:
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        updated = store.links(self.target.target_id)["legacy_entrance"]
        self.assertEqual(self.repo.latest_camera_address_revision(row["camera_uuid"]), updated.get("relay_address_revision"))
        self.assertEqual(sorted(row["managed_streams"]), [call.args[0] for call in delete.call_args_list])
        self.assertEqual(sorted(row["managed_streams"]), sorted(alias for alias, _ in self.client.runtime_writes))
        self.assertEqual(("set_runtime_camera_enabled", "legacy_entrance", False), self.client.operations[0])
        self.assertEqual(("set_runtime_camera_enabled", "legacy_entrance", True), self.client.operations[-1])
        operations = list(self.client.operations)
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(operations, self.client.operations)
        self.assertEqual(before, self.client.raw_config())

    def test_reader_replacement_failure_keeps_revision_pending_and_restores_capture(self):
        self.execute(self.preview())
        store = ImportStore(self.repo)
        row = store.links(self.target.target_id)["legacy_entrance"]
        self.repo.record_address_change(row["camera_uuid"], "192.0.2.10", "192.0.2.11", "onvif_identity")
        before = self.client.raw_config()
        self.client.runtime_writes.clear()
        replace = self.client.set_runtime_stream
        def fail(alias, source):
            replace(alias, source)
            raise FrigateApiError("request_rejected", upstream_status=400, upstream_detail="synthetic permission error")
        with patch.object(self.client, "set_runtime_stream", side_effect=fail):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(set(row["managed_streams"]), {alias for alias, _ in self.client.runtime_writes})
        self.assertNotIn("relay_address_revision", store.links(self.target.target_id)["legacy_entrance"])
        self.assertTrue(self.client.config()["cameras"]["legacy_entrance"]["enabled"])
        self.assertEqual(before, self.client.raw_config())

    def test_missing_yaml_cleanup_is_repaired_with_one_seed_and_delete_retry(self):
        self.execute(self.preview())
        store = ImportStore(self.repo)
        row = store.links(self.target.target_id)["legacy_entrance"]
        aliases = sorted(row["managed_streams"])
        self.repo.record_address_change(row["camera_uuid"], "192.0.2.10", "192.0.2.11", "onvif_identity")
        before = self.client.raw_config()
        runtime = {alias: {} for alias in aliases}
        deleted, seeded = [], []
        def delete(alias):
            deleted.append(alias)
            if alias == aliases[0] and alias not in seeded:
                raise FrigateApiError("request_rejected", upstream_status=400,
                                     upstream_detail="Failed to delete stream: yaml: path not exist")
            runtime.pop(alias, None)
        def replace(alias, source):
            self.assertEqual(row["managed_streams"][alias][0], source)
            seeded.append(alias)
            runtime[alias] = {}
        with patch.object(self.client, "runtime_streams", side_effect=lambda: copy.deepcopy(runtime)), \
             patch.object(self.client, "delete_runtime_stream", side_effect=delete), \
             patch.object(self.client, "set_runtime_stream", side_effect=replace):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
            self.assertEqual([aliases[0], *aliases], deleted)
            self.assertEqual(set(aliases), set(runtime))
            self.assertEqual(self.repo.latest_camera_address_revision(row["camera_uuid"]),
                             store.links(self.target.target_id)["legacy_entrance"].get("relay_address_revision"))
            operations = list(self.client.operations)
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
            self.assertEqual(operations, self.client.operations)
        self.assertEqual(before, self.client.raw_config())

    def test_missing_yaml_cleanup_retry_is_bounded_and_capture_resumes(self):
        self.execute(self.preview())
        store = ImportStore(self.repo)
        row = store.links(self.target.target_id)["legacy_entrance"]
        self.repo.record_address_change(row["camera_uuid"], "192.0.2.10", "192.0.2.11", "onvif_identity")
        before = self.client.raw_config()
        with patch.object(self.client, "delete_runtime_stream", side_effect=FrigateApiError(
                "request_rejected", upstream_status=400, upstream_detail="yaml: path not exist")) as delete:
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(2, delete.call_count)
        self.assertNotIn("relay_address_revision", store.links(self.target.target_id)["legacy_entrance"])
        self.assertTrue(self.client.config()["cameras"]["legacy_entrance"]["enabled"])
        self.assertEqual(before, self.client.raw_config())

    def test_address_refresh_retries_ambiguous_replacement_and_preserves_sources(self):
        job = self.execute(self.preview())
        uid = job["cameras"][0]["camera_uuid"]
        store = ImportStore(self.repo)
        row = store.links(self.target.target_id)["legacy_entrance"]
        aliases = sorted(row["managed_streams"])
        before = self.client.raw_config()
        self.repo.record_address_change(uid, "192.0.2.10", "192.0.2.11", "onvif_identity")
        self.client.runtime_writes.clear()
        replace = self.client.set_runtime_stream
        def ambiguous(alias, source):
            replace(alias, source)
            if alias == aliases[0]:
                raise FrigateApiError("target_unavailable")
        with patch.object(self.client, "set_runtime_stream", side_effect=ambiguous):
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(set(aliases), {a for a, _ in self.client.runtime_writes})
        self.assertNotIn("relay_address_revision", store.links(self.target.target_id)["legacy_entrance"])
        self.assertEqual(before, self.client.raw_config())
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual(self.repo.latest_camera_address_revision(uid),
                         store.links(self.target.target_id)["legacy_entrance"]["relay_address_revision"])

    def test_address_refresh_respects_operator_source_conflicts(self):
        job = self.execute(self.preview())
        uid = job["cameras"][0]["camera_uuid"]
        row = ImportStore(self.repo).links(self.target.target_id)["legacy_entrance"]
        alias = next(iter(row["managed_streams"]))
        self.client.current_raw_config["go2rtc"]["streams"][alias] = ["rtsp://192.0.2.44/operator"]
        self.repo.record_address_change(uid, "192.0.2.10", "192.0.2.11", "onvif_identity")
        self.client.operations.clear()
        before = self.client.raw_config()
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual([], self.client.operations)
        self.assertEqual(before, self.client.raw_config())

    def test_tentative_source_change_does_not_refresh_imported_readers(self):
        job = self.execute(self.preview())
        uid = job["cameras"][0]["camera_uuid"]
        self.assertIsNone(self.repo.latest_camera_address_revision(uid))
        self.client.operations.clear()
        # A desired revision is not a successfully committed address recovery.
        self.repo.record_desired_media_revision(self.repo.managed_stream_sources())
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual([], self.client.operations)

    def test_address_refresh_skips_disabled_and_unselected_cameras(self):
        job = self.execute(self.preview())
        uid = job["cameras"][0]["camera_uuid"]
        self.repo.record_address_change(uid, "192.0.2.10", "192.0.2.11", "onvif_identity")
        self.repo.set_camera_enabled(uid, False)
        self.client.operations.clear()
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual([], self.client.operations)
        self.repo.set_camera_enabled(uid, True)
        self.repo.deselect_frigate_camera(self.target.target_id, uid)
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: "192.0.2.20")
        self.assertEqual([], self.client.operations)

    def test_existing_camera_is_reused_and_conflicting_roles_require_review(self):
        self.execute(self.preview())
        restore(self.repo, self.target, 'legacy_entrance', self.client)
        camera = self.repo.consumer_inventory()[0]
        with self.repo.connect() as c:
            c.execute('UPDATE cameras SET display_name=?', ('Different CamAdmiral name',))
            c.commit()
        job = self.preview()
        self.assertEqual('existing', job['cameras'][0]['category'])
        self.assertEqual(camera['camera_uuid'], job['cameras'][0]['camera_uuid'])
        self.execute(job)
        self.assertEqual(1, len(self.repo.consumer_inventory()))
        self.assertEqual('Original entrance', self.client.raw_config()['cameras']['legacy_entrance']['friendly_name'])

    def test_ambiguous_partial_profile_match_and_shared_live_alias_are_conflicts(self):
        self.execute(self.preview())
        restore(self.repo, self.target, 'legacy_entrance', self.client)
        camera = copy.deepcopy(self.camera)
        camera['ffmpeg']['inputs'].append({'path': 'rtsp://operator:synthetic-password@192.0.2.10/other', 'roles': ['detect']})
        self.client.seed('legacy_entrance', camera)
        self.assertEqual('ambiguous_source_match', self.preview()['cameras'][0]['reason'])
        camera = copy.deepcopy(self.camera)
        camera['live'] = {'streams': {'Main': 'shared'}}
        self.client.seed('legacy_entrance', camera, {'shared': ['rtsp://192.0.2.10/main']})
        self.client.seed('other_entry', camera)
        self.assertTrue(all(r['reason'] == 'shared_live_alias' for r in self.preview()['cameras']))

    def test_credential_conflict_requires_explicit_keep_existing(self):
        self.execute(self.preview())
        restore(self.repo, self.target, 'legacy_entrance', self.client)
        camera = copy.deepcopy(self.camera)
        camera['ffmpeg']['inputs'][0]['path'] = 'rtsp://other:synthetic-other-secret@192.0.2.10/main'
        self.client.seed('legacy_entrance', camera)
        job = self.preview()
        self.assertEqual('existing_settings_differ', job['cameras'][0]['reason'])
        self.assertTrue(job['cameras'][0]['can_keep_existing'])
        result = self.execute(job)
        self.assertEqual('pending', result['cameras'][0]['status'])
        result = self.execute(result, keep=['legacy_entrance'])
        self.assertEqual('applied', result['cameras'][0]['status'])
        source = self.repo.managed_stream_sources()[0]
        self.assertEqual('operator', source['username'])
        self.assertEqual('synthetic-password', source['password'])

    def test_interrupted_jobs_recover_without_plaintext_or_lost_bindings(self):
        job = self.preview()
        job['status'] = 'running'
        store = ImportStore(self.repo)
        store.save_job(self.target.target_id, job)
        store.interrupt_jobs()
        restored = store.job(self.target.target_id, job['id'])
        self.assertEqual('error', restored['status'])
        self.assertEqual('interrupted_import', restored['cameras'][0]['reason'])
        self.assertEqual('applied', self.execute(restored)['cameras'][0]['status'])

    def test_external_source_edits_block_sync_and_rollback(self):
        self.execute(self.preview())
        camera = self.client.current_raw_config['cameras']['legacy_entrance']
        camera['ffmpeg']['inputs'][0]['path'] = 'rtsp://192.0.2.99/operator-change'
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda mode: '192.0.2.20')
        with self.assertRaises(FrigateApiError) as error:
            restore(self.repo, self.target, 'legacy_entrance', self.client)
        self.assertEqual('rollback_configuration_conflict', error.exception.code)
        self.assertEqual('rtsp://192.0.2.99/operator-change', self.client.raw_config()['cameras']['legacy_entrance']['ffmpeg']['inputs'][0]['path'])

    def test_removal_requires_explicit_restore_without_hidden_restart(self):
        from camadmiral.frigate import remove_frigate_camera
        job = self.execute(self.preview())
        writes, restarts = len(self.client.config_writes), self.client.restart_calls
        with self.assertRaises(FrigateApiError) as error:
            remove_frigate_camera(self.repo, self.target, job['cameras'][0]['camera_uuid'],
                                 client_factory=lambda target: self.client)
        self.assertEqual('import_restore_required', error.exception.code)
        self.assertEqual(writes, len(self.client.config_writes))
        self.assertEqual(restarts, self.client.restart_calls)

    def test_implicit_live_alias_is_imported_and_late_sharing_blocks_takeover(self):
        data = inspect_camera('legacy_entrance', self.camera, self.camera,
                              {'legacy_entrance': ['rtsp://operator:synthetic-password@192.0.2.10/main']})
        self.assertIn('legacy_entrance', data['live'])
        camera = copy.deepcopy(self.camera)
        camera['live'] = {'streams': {'Main': 'entrance_live'}}
        self.client.seed('legacy_entrance', camera, {'entrance_live': ['rtsp://operator:synthetic-password@192.0.2.10/main']})
        job = self.preview()
        other = copy.deepcopy(camera)
        self.client.seed('late_added_camera', other)
        result = self.execute(job)
        self.assertEqual('configuration_changed_refresh_preview', result['cameras'][0]['reason'])
        self.assertEqual([], self.client.config_writes)

    def test_adoption_acknowledgement_loss_reuses_deterministic_candidate(self):
        job = self.execute(self.preview())
        cid = job['cameras'][0]['camera_uuid']
        restore(self.repo, self.target, 'legacy_entrance', self.client)
        job['cameras'][0].update(camera_uuid=None, status='error')
        ImportStore(self.repo).save_job(self.target.target_id, job)
        result = self.execute(job)
        self.assertEqual('applied', result['cameras'][0]['status'])
        self.assertEqual(cid, result['cameras'][0]['camera_uuid'])
        self.assertEqual(1, len(self.repo.consumer_inventory()))

    def test_nonstandard_secret_paths_are_not_stored_as_plaintext_sources(self):
        for uri in ('rtsp://192.0.2.1/main?pass=synthetic-secret',
                    'rtsp://192.0.2.1/password=synthetic-secret/main'):
            with self.subTest(uri=uri), self.assertRaises(FrigateApiError) as error:
                resolve_source(uri, {})
            self.assertEqual('unsupported_secret_query', error.exception.code)

    def test_ready_metadata_without_decoded_media_never_switches_frigate(self):
        from camadmiral.media import SnapshotError
        job = self.preview()
        with patch('camadmiral.frigate_import.probe_source', return_value=ProbeResult('ready', 1, 'h264', width=640, height=360)), \
             patch('camadmiral.frigate_import._target_host', return_value='192.0.2.20'), \
             patch('camadmiral.frigate_import.snapshot_frame', side_effect=SnapshotError('Snapshot is unavailable')):
            result = execute(self.repo, self.target, job['id'], ['legacy_entrance'], [], None, lambda: True, self.client)
        self.assertEqual('relay_video_unavailable', result['cameras'][0]['reason'])
        self.assertEqual([], self.client.config_writes)
        self.assertEqual(self.camera, self.client.raw_config()['cameras']['legacy_entrance'])
