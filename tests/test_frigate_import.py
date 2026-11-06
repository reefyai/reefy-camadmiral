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
                                      execute, restore, sync_imported, inspect_camera, start_job)
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


class FrigateImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = CameraRepository(Path(self.tmp.name) / 'camera.db', b'x' * 32)
        self.repo.migrate()
        self.repo.save_frigate_target('synthetic-target', 'Synthetic target', 'http://frigate:5000')
        self.target = FrigateTarget('synthetic-target', 'Synthetic target', 'http://frigate:5000')
        self.client = ImportClient(self.target)
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

    def execute(self, job, selected=None, keep=()):
        with patch('camadmiral.frigate_import.probe_source', return_value=ProbeResult('ready', 1, 'h264', width=640, height=360, fps=10)), \
             patch('camadmiral.frigate_import._target_host', return_value='192.0.2.20'), \
             patch('camadmiral.frigate_import.snapshot_frame', return_value=b'\xff\xd8\xffsynthetic\xff\xd9'):
            return execute(self.repo, self.target, job['id'], selected or ['legacy_entrance'], list(keep), None,
                           lambda: True, self.client)

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
        job['cameras'][0].update(status='error', reason='source_unavailable')
        start_job(job, ['legacy_entrance'], ['legacy_entrance'])
        result = public_job(job)
        self.assertEqual(['legacy_entrance'], result['selected'])
        self.assertEqual(['legacy_entrance'], result['keep_existing'])
        self.assertEqual('queued', result['cameras'][0]['status'])
        self.assertIsNone(result['cameras'][0]['reason'])
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
