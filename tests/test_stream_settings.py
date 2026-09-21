import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camadmiral.storage import CameraRepository
from camadmiral.media import ProbeResult
from camadmiral.recovery import _validated_updates


class StreamSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = CameraRepository(Path(temp.name) / 'state.db', b'k' * 32)
        self.repo.migrate()
        self.candidate = {'candidate_uuid': 'synthetic-streams', 'ip': '192.168.1.2'}
        self.profiles = [{'token': name, 'name': name, 'uri': f'rtsp://192.168.1.2/{name}',
                          'source_kind': 'manual_rtsp'} for name in ('main', 'sub')]
        self.camera = self.repo.adopt(self.candidate, 'test', 'secret', self.profiles,
                                     {'record': 'main', 'detect': 'sub'})
        self.cid = self.camera['camera_uuid']
        self.main = self.camera['roles']['record']
        self.sub = self.camera['roles']['detect']

    def save(self):
        self.repo.set_stream_settings(self.cid, [self.sub], {'record': self.sub, 'detect': self.sub})

    def test_disabled_stream_excluded_from_every_reader_and_reenable_preserves_urls(self):
        self.save()
        self.assertEqual([s['stream_uuid'] for s in self.repo.managed_stream_sources()], [self.sub])
        self.assertEqual([s['stream_uuid'] for s in self.repo.managed_stream_runtime_sources()], [self.sub])
        self.assertEqual([s['stream_uuid'] for s in self.repo.consumer_inventory()[0]['streams']], [self.sub])
        self.assertEqual(self.repo.preview_stream_for_camera(self.cid)['stream_uuid'], self.sub)
        self.repo.record_probe_results({self.main: ProbeResult('auth_failed', 1)})
        self.assertNotIn(self.main, self.repo.auth_retry_schedule())
        self.repo.set_stream_settings(self.cid, [self.main, self.sub], self.camera['roles'])
        now = self.repo.adoption_for_candidate(self.candidate['candidate_uuid'])
        self.assertEqual([(s['stream_uuid'], s['stream_key']) for s in now['streams']],
                         [(s['stream_uuid'], s['stream_key']) for s in self.camera['streams']])

    def test_invalid_settings_are_atomic(self):
        for enabled, roles in [([], self.camera['roles']), ([self.sub], self.camera['roles']),
                               ([self.main, self.sub], {'record': self.main}),
                               (['other-camera'], {'record': 'other-camera', 'detect': 'other-camera'})]:
            with self.assertRaises(ValueError):
                self.repo.set_stream_settings(self.cid, enabled, roles)
            self.assertEqual(self.repo.adoption_for_candidate(self.candidate['candidate_uuid']), self.camera)

    def test_settings_survive_reopen_and_profile_refresh(self):
        self.save()
        reopened = CameraRepository(self.repo.path, b'k' * 32)
        reopened.migrate()
        updated = reopened.adopt(self.candidate, 'test', 'secret', self.profiles,
                                 {'record': 'main', 'detect': 'sub'})
        self.assertEqual(updated['roles'], {'record': self.sub, 'detect': self.sub})
        self.assertFalse(next(s for s in updated['streams'] if s['stream_uuid'] == self.main)['enabled'])

    def test_upgrade_defaults_existing_streams_to_enabled_without_identity_changes(self):
        from camadmiral.storage import MIGRATIONS
        before = self.repo.managed_stream_sources()
        with self.repo.connect() as connection:
            connection.execute('ALTER TABLE managed_streams DROP COLUMN enabled')
            connection.execute('ALTER TABLE cameras DROP COLUMN stream_settings_custom')
            version = next(i for i, sql in enumerate(MIGRATIONS, 1) if 'ADD COLUMN stream_settings_custom' in sql)
            connection.execute('DELETE FROM schema_migrations WHERE version=?', (version,))
            connection.commit()
        self.repo.migrate()
        self.assertEqual(self.repo.managed_stream_sources(), before)
        self.assertEqual(self.repo.adoption_for_candidate(self.candidate['candidate_uuid']), self.camera)

    def test_missing_disabled_profile_keeps_reserved_stream_identity(self):
        self.save()
        updated = self.repo.adopt(self.candidate, 'test', 'secret', self.profiles[1:],
                                  {'record': 'sub', 'detect': 'sub'})
        self.assertEqual({s['stream_uuid'] for s in updated['streams']}, {self.main, self.sub})
        self.assertFalse(next(s for s in updated['streams'] if s['stream_uuid'] == self.main)['enabled'])

    def test_address_recovery_updates_disabled_uri_without_probing_it(self):
        self.save()
        adoption = self.repo.adoption_for_candidate(self.candidate['candidate_uuid'])
        with patch('camadmiral.recovery.probe_source', return_value=ProbeResult('ready', 1)) as probe:
            updates, status = _validated_updates({'ip': '192.168.1.3'}, adoption, 'test', 'secret')
        self.assertEqual(status, 'ready')
        self.assertEqual(set(updates), {'main', 'sub'})
        probe.assert_called_once_with('rtsp://192.168.1.3/sub', 'test', 'secret')

    def test_save_reconciles_before_one_restart_and_queues_frigate(self):
        from camadmiral import app
        with patch.object(app, '_repository', return_value=self.repo), \
             patch.object(app, 'reconcile_streams') as reconcile, \
             patch.object(app, 'replace_streams') as restart, \
             patch.object(app, '_queue_frigate_reconciliation') as frigate:
            result = app.set_camera_stream_settings(self.cid, app.CameraStreamSettingsRequest(
                enabled=[self.sub], roles={'record': self.sub, 'detect': self.sub}),
                x_camadmiral_action='set-camera-stream-settings')
        self.assertEqual(result.status_code, 200)
        reconcile.assert_called_once()
        restart.assert_called_once_with(reconcile.call_args.args[0])
        self.assertEqual([s['stream_uuid'] for s in restart.call_args.args[0]], [self.sub])
        frigate.assert_called_once()

    def test_invalid_save_does_not_restart(self):
        from camadmiral import app
        from fastapi import HTTPException
        with patch.object(app, '_repository', return_value=self.repo), \
             patch.object(app, 'replace_streams') as restart:
            with self.assertRaises(HTTPException) as caught:
                app.set_camera_stream_settings(self.cid, app.CameraStreamSettingsRequest(
                    enabled=[self.sub], roles=self.camera['roles']),
                    x_camadmiral_action='set-camera-stream-settings')
        self.assertEqual(caught.exception.status_code, 400)
        restart.assert_not_called()


if __name__ == '__main__':
    unittest.main()
