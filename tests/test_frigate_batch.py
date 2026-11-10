"""Grouped cutover failure boundaries and durable, private recovery."""
import copy
from contextlib import ExitStack
import unittest
from unittest.mock import patch

import test_frigate_import as fixtures
from camadmiral.frigate import FrigateApiError, FrigateTarget
from camadmiral.frigate_batch import BatchCutover
from camadmiral.frigate_import import ImportStore, execute, recover_handoffs, public_job
from camadmiral.media import ProbeResult


class ProcessLost(BaseException):
    pass


class FrigateBatchTests(unittest.TestCase):
    setUp = fixtures.FrigateImportTests.setUp
    preview = fixtures.FrigateImportTests.preview

    def seed_pair(self):
        second = copy.deepcopy(self.camera)
        second['ffmpeg']['inputs'][0]['path'] = 'rtsp://operator:synthetic-password@192.0.2.11/main'
        self.client.seed('second_camera', second)
        return self.preview()

    def media(self):
        stack = ExitStack()
        stack.enter_context(patch('camadmiral.frigate_import.probe_source', return_value=ProbeResult(
            'ready', 1, 'h264', width=640, height=360, fps=10)))
        stack.enter_context(patch('camadmiral.frigate_import._target_host', return_value='192.0.2.20'))
        stack.enter_context(patch('camadmiral.frigate_import.snapshot_frame', return_value=b'frame'))
        return stack

    def run_pair(self, job):
        return execute(self.repo, self.target, job['id'], ['legacy_entrance', 'second_camera'],
                       [], None, lambda: True, self.client)

    def recover(self):
        ImportStore(self.repo).interrupt_jobs()
        recover_handoffs(self.repo, lambda: True, lambda target: self.client)

    def test_missing_runtime_controls_use_two_shared_restarts(self):
        job = self.seed_pair()
        with self.media(), patch.object(self.client, 'set_runtime_camera_enabled',
                side_effect=FrigateApiError('target_unavailable')):
            result = self.run_pair(job)
        self.assertEqual(['applied', 'applied'], [r['status'] for r in result['cameras']])
        self.assertEqual(2, self.client.restart_calls)
        self.assertEqual(2, len(self.client.restart_config_writes))

    def test_failed_relay_restores_only_failed_camera_and_preserves_adoption(self):
        job = self.seed_pair()
        before = self.client.raw_config()
        validate = BatchCutover.validate
        def one_broken(batch, row):
            if row['key'] == 'second_camera':
                raise FrigateApiError('relay_video_unavailable')
            return validate(batch, row)
        with self.media(), patch.object(BatchCutover, 'validate', one_broken):
            result = self.run_pair(job)
        rows = {r['key']: r for r in result['cameras']}
        self.assertEqual('applied', rows['legacy_entrance']['status'])
        self.assertEqual('relay_video_unavailable', rows['second_camera']['reason'])
        self.assertEqual('restored', rows['second_camera']['configuration_status'])
        self.assertEqual(before['cameras']['second_camera'], self.client.raw_config()['cameras']['second_camera'])
        self.assertEqual({'legacy_entrance'}, set(ImportStore(self.repo).links(self.target.target_id)))
        self.assertEqual(2, len(self.repo.consumer_inventory()), 'Failed adoption data must remain available')
        enabled = {c['camera_uuid']: c['enabled'] for c in self.repo.consumer_inventory()}
        self.assertFalse(enabled[rows['second_camera']['camera_uuid']])
        self.assertTrue(enabled[rows['legacy_entrance']['camera_uuid']])
        self.assertEqual(1, self.client.restart_calls)

    def test_process_loss_after_release_restores_whole_group_without_restart(self):
        job = self.seed_pair()
        before = self.client.raw_config()
        with self.media(), patch.object(BatchCutover, 'stage', side_effect=ProcessLost()):
            with self.assertRaises(ProcessLost): self.run_pair(job)
        self.assertFalse(any(c['enabled'] for c in self.client.config()['cameras'].values()))
        self.recover()
        self.assertEqual(before, self.client.raw_config())
        self.assertTrue(all(c['enabled'] for c in self.client.config()['cameras'].values()))
        self.assertEqual({}, ImportStore(self.repo).links(self.target.target_id))
        self.assertEqual(0, self.client.restart_calls)
        restored = ImportStore(self.repo).job(self.target.target_id, job['id'])
        self.assertEqual('complete', restored['batch']['phase'])

    def test_accepted_restart_with_process_loss_and_api_outage_is_not_duplicated(self):
        job = self.seed_pair()
        restart, raw = self.client.restart, self.client.raw_config
        def lost():
            restart()
            raise ProcessLost()
        with self.media(), patch.object(self.client, 'restart', side_effect=lost):
            with self.assertRaises(ProcessLost): self.run_pair(job)
        saved = ImportStore(self.repo).job(self.target.target_id, job['id'])
        identities = {r['key']: r['camera_uuid'] for r in saved['cameras']}
        clock = [0.0]
        def offline():
            if clock[0] < 20: raise FrigateApiError('target_unavailable')
            return raw()
        def sleep(seconds): clock[0] += seconds
        with patch.object(self.client, 'raw_config', side_effect=offline), \
             patch('camadmiral.frigate_batch.time.monotonic', side_effect=lambda: clock[0]), \
             patch('camadmiral.frigate_batch.time.sleep', side_effect=sleep):
            self.recover()
        result = ImportStore(self.repo).job(self.target.target_id, job['id'])
        self.assertEqual('completed', result['status'])
        self.assertEqual(['applied', 'applied'], [r['status'] for r in result['cameras']])
        self.assertEqual(identities, {r['key']: r['camera_uuid'] for r in result['cameras']})
        self.assertEqual(1, self.client.restart_calls)
        self.assertGreaterEqual(clock[0], 20)

    def test_process_loss_after_group_save_completes_one_cutover(self):
        job = self.seed_pair()
        def interrupt(batch, kind):
            self.assertEqual('cutover', kind)
            self.assertTrue(all(row['phase'] == 'waiting_for_frigate' for row in batch.rows))
            raise ProcessLost()
        with self.media(), patch.object(BatchCutover, 'restart', interrupt):
            with self.assertRaises(ProcessLost): self.run_pair(job)
        self.assertEqual(0, self.client.restart_calls)
        self.recover()
        result = ImportStore(self.repo).job(self.target.target_id, job['id'])
        self.assertEqual(['applied', 'applied'], [r['status'] for r in result['cameras']])
        self.assertEqual(1, self.client.restart_calls)

    def test_unrelated_runtime_controls_are_preserved_without_import_changes(self):
        job = self.seed_pair()
        untouched = copy.deepcopy(self.camera)
        untouched['ffmpeg']['inputs'][0]['path'] = 'rtsp://192.0.2.12/main'
        self.client.seed('untouched', untouched)
        self.client.current_config['cameras']['untouched']['record']['enabled'] = False
        raw = self.client.raw_config()
        with self.media(): result = self.run_pair(job)
        self.assertTrue(all(r['reason'] == 'import_source_conflict' for r in result['cameras']))
        self.assertFalse(self.client.config()['cameras']['untouched']['record']['enabled'])
        self.assertEqual(raw, self.client.raw_config())
        self.assertEqual([], self.client.operations)
        self.assertEqual([], self.repo.consumer_inventory())

    def test_operator_edit_during_group_preparation_is_preserved(self):
        job = self.seed_pair()
        original = self.client.raw_config()
        with self.media(), patch.object(BatchCutover, 'stage', side_effect=ProcessLost()):
            with self.assertRaises(ProcessLost): self.run_pair(job)
        self.client.current_raw_config['cameras']['second_camera']['detect']['fps'] = 9
        self.recover()
        self.assertEqual(9, self.client.raw_config()['cameras']['second_camera']['detect']['fps'])
        self.assertEqual(original['cameras']['legacy_entrance'], self.client.raw_config()['cameras']['legacy_entrance'])
        links = ImportStore(self.repo).links(self.target.target_id)
        self.assertEqual({'second_camera'}, set(links))
        self.assertEqual('rollback_required', links['second_camera']['reason'])
        self.assertEqual(0, self.client.restart_calls)

    def test_recovery_does_not_write_to_changed_target(self):
        job = self.seed_pair()
        with self.media(), patch.object(BatchCutover, 'stage', side_effect=ProcessLost()):
            with self.assertRaises(ProcessLost): self.run_pair(job)
        self.repo.save_frigate_target(self.target.target_id, 'Replacement', 'http://other-frigate:5000')
        writes = list(self.client.operations)
        self.recover()
        self.assertEqual(writes, self.client.operations)
        self.assertEqual('staging', ImportStore(self.repo).job(self.target.target_id, job['id'])['batch']['phase'])

    def test_pending_group_journal_survives_preview_pruning_and_is_private(self):
        job = self.seed_pair()
        with self.media(), patch.object(BatchCutover, 'stage', side_effect=ProcessLost()):
            with self.assertRaises(ProcessLost): self.run_pair(job)
        for _ in range(22): self.preview()
        saved = ImportStore(self.repo).job(self.target.target_id, job['id'])
        self.assertEqual('staging', saved['batch']['phase'])
        public = public_job(saved)
        self.assertNotIn('batch', public)
        self.assertNotIn('synthetic-password', str(public))
        self.recover()
        self.assertEqual({}, ImportStore(self.repo).links(self.target.target_id))

    def test_scalar_alias_sources_are_transferred_whole_and_restored(self):
        self.seed_pair()
        sources = {}
        for index, key in enumerate(('legacy_entrance', 'second_camera')):
            camera = copy.deepcopy(self.camera)
            alias = 'original_' + str(index)
            sources[alias] = 'rtsp://operator:synthetic-password@192.0.2.' + str(10 + index) + '/main'
            camera['ffmpeg']['inputs'][0]['path'] = 'rtsp://127.0.0.1:8554/' + alias
            camera['live'] = {'streams': {'Main': alias}}
            self.client.seed(key, camera, {alias: sources[alias]})
        job, validate = self.preview(), BatchCutover.validate
        before = self.client.raw_config()
        deleted = []
        def delete(client, alias, source):
            self.assertEqual(sources[alias], source)
            deleted.append(alias)
        def fail_second(batch, row):
            if row['key'] == 'second_camera': raise FrigateApiError('relay_video_unavailable')
            return validate(batch, row)
        with self.media(), patch('camadmiral.frigate_import._delete_runtime_alias', side_effect=delete), \
             patch.object(BatchCutover, 'validate', fail_second):
            result = self.run_pair(job)
        self.assertEqual(['applied', 'error'], [row['status'] for row in result['cameras']])
        self.assertEqual(set(sources), set(deleted))
        self.assertIn(('original_1', sources['original_1']), self.client.runtime_writes)
        self.assertEqual(before['cameras']['second_camera'], self.client.raw_config()['cameras']['second_camera'])
        self.assertEqual(before['go2rtc']['streams']['original_1'], self.client.raw_config()['go2rtc']['streams']['original_1'])
        self.assertEqual(1, self.client.restart_calls)

    def test_api_ready_before_first_frames_waits_for_original_feeds(self):
        job = self.seed_pair()
        clock = [0.0]
        stats = self.client.stats
        def warming():
            return stats() if clock[0] >= 3 else {key: {'camera_fps': 0} for key in stats()}
        def sleep(seconds): clock[0] += seconds
        with self.media(), patch.object(self.client, 'stats', side_effect=warming), \
             patch('camadmiral.frigate_batch.time.monotonic', side_effect=lambda: clock[0]), \
             patch('camadmiral.frigate_batch.time.sleep', side_effect=sleep):
            result = self.run_pair(job)
        self.assertGreaterEqual(clock[0], 3)
        self.assertEqual(['applied', 'applied'], [row['status'] for row in result['cameras']])
        self.assertEqual(1, self.client.restart_calls)

    def test_offline_originals_remain_unchanged_after_shared_readiness_budget(self):
        job = self.seed_pair()
        clock = [0.0]
        before = self.client.raw_config()
        def sleep(seconds): clock[0] += seconds
        with self.media() as media, patch.object(self.client, 'stats', return_value={}), \
             patch('camadmiral.frigate_batch.time.monotonic', side_effect=lambda: clock[0]), \
             patch('camadmiral.frigate_batch.time.sleep', side_effect=sleep):
            result = self.run_pair(job)
        self.assertEqual(90, clock[0], 'Offline peers share one readiness budget')
        self.assertTrue(all(row['reason'] == 'source_unavailable' for row in result['cameras']))
        self.assertEqual(before, self.client.raw_config())
        self.assertEqual([], self.client.operations)
        self.assertEqual([], self.repo.consumer_inventory())

    def test_implicit_enabled_cameras_clear_persisted_runtime_pause_on_cutover(self):
        self.seed_pair()
        implicit = copy.deepcopy(self.camera)
        implicit.pop('enabled')
        implicit['ffmpeg']['inputs'][0]['path'] = 'rtsp://operator:synthetic-password@192.0.2.11/main'
        self.client.seed('second_camera', implicit)
        job = self.preview()
        restart, save = self.client.restart, self.client.set_config
        def clear_saved_overrides(payload, **kwargs):
            save(payload, **kwargs)
            for key, update in payload.get('cameras', {}).items():
                if 'enabled' in update:
                    self.client.runtime_enabled.pop(key, None)
        def persistent_runtime_restart():
            restart()
            for key, camera in self.client.current_raw_config['cameras'].items():
                if 'enabled' not in camera and not self.client.runtime_enabled.get(key, True):
                    self.client.current_config['cameras'][key]['enabled'] = False
                    self.client.current_stats[key]['camera_fps'] = 0
        clock = [0.0]
        def sleep(seconds): clock[0] += seconds
        with self.media(), patch.object(self.client, 'restart', side_effect=persistent_runtime_restart), \
             patch.object(self.client, 'set_config', side_effect=clear_saved_overrides), \
             patch('camadmiral.frigate_batch.time.monotonic', side_effect=lambda: clock[0]), \
             patch('camadmiral.frigate_batch.time.sleep', side_effect=sleep):
            result = self.run_pair(job)
        self.assertEqual(['applied', 'applied'], [row['status'] for row in result['cameras']])
        self.assertTrue(self.client.raw_config()['cameras']['second_camera']['enabled'])
        self.assertEqual(implicit['record'], self.client.raw_config()['cameras']['second_camera']['record'])
        self.assertEqual(1, self.client.restart_calls)
        with patch.object(self.client, 'set_config', side_effect=clear_saved_overrides), \
             patch.object(self.client, 'restart', side_effect=persistent_runtime_restart):
            fixtures.restore(self.repo, self.target, 'second_camera', self.client, lambda: True)
        self.assertEqual(implicit, self.client.raw_config()['cameras']['second_camera'])


class FailureDiagnosticsTests(unittest.TestCase):
    def test_database_error_codes_are_logged_without_exception_secrets(self):
        import contextlib
        import io
        import sqlite3
        from camadmiral.frigate_batch import _failure_event
        exc = sqlite3.OperationalError('rtsp://operator:synthetic-secret@192.0.2.90/live')
        exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
        exc.sqlite_errorname = 'SQLITE_BUSY'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _failure_event('row_failed', 'saving', exc, {'camera_uuid': 'synthetic-camera'})
        value = output.getvalue()
        self.assertIn('"sqlite_errorcode": 5', value)
        self.assertIn('"sqlite_errorname": "SQLITE_BUSY"', value)
        self.assertNotIn('synthetic-secret', value)
        self.assertNotIn('rtsp://', value)
