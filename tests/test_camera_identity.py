import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from camadmiral import app as app_module
from camadmiral.camera_identity import (
    LEARN_ATTEMPTS, discover_identity, enrich_existing_imports,
    learn_import_identity, linked_candidate, same_identity,
)
from camadmiral.media import ProbeResult
from camadmiral.recovery import ATTEMPTED_AT, ATTEMPT_COUNTS, recover_inventory_addresses
from camadmiral.storage import CameraRepository


def device(address='192.168.50.10', *, endpoint='urn:uuid:synthetic-device', mac='02:00:00:00:50:10'):
    return {'candidate_uuid': 'physical-device', 'ip': address, 'mac': mac,
            'onvif': {'endpoint_reference': endpoint}, 'status': 'online',
            'last_seen': '2099-01-01T00:00:00+00:00'}


class ImportedIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = CameraRepository(self.root / 'database.db', b'k' * 32)
        self.repository.migrate()
        self.inventory = self.root / 'inventory.json'
        self.inventory.write_text(json.dumps({'devices': []}))
        LEARN_ATTEMPTS.clear()
        ATTEMPTED_AT.clear()
        ATTEMPT_COUNTS.clear()

    def tearDown(self):
        self.temporary.cleanup()
        LEARN_ATTEMPTS.clear()
        ATTEMPTED_AT.clear()
        ATTEMPT_COUNTS.clear()

    def adopt(self, channel='main'):
        return self.repository.adopt(
            {'candidate_uuid': 'frigate-import:synthetic:' + channel,
             'display_name': 'Synthetic ' + channel, 'ip': '192.168.50.10'},
            'viewer', 'synthetic-secret',
            [{'token': channel, 'name': channel, 'uri': 'rtsp://192.168.50.10:8554/' + channel + '?quality=main',
              'width': 640, 'height': 360, 'encoding': 'H264', 'fps': 10,
              'bitrate_kbps': 0, 'source_kind': 'manual_rtsp'}],
            {'record': channel, 'detect': channel}, camera_origin='direct')

    @patch('camadmiral.camera_identity.scan_explicit_address')
    def test_import_learns_fresh_identity_without_replacing_credentials_or_ids(self, scan):
        before = self.adopt()
        scan.return_value = {'devices': [device()]}
        self.assertTrue(learn_import_identity(self.repository, before['camera_uuid'], self.inventory))
        after = self.repository.adoption_for_candidate('frigate-import:synthetic:main')
        self.assertEqual(before, after)
        self.assertEqual(self.repository.credentials_for_candidate(after['candidate_uuid']), ('viewer', 'synthetic-secret'))
        self.assertEqual(self.repository.camera_identity_history(before['camera_uuid'])[0]['onvif_identity'], 'urn:uuid:synthetic-device')
        self.assertFalse(learn_import_identity(self.repository, before['camera_uuid'], self.inventory))
        self.assertEqual(scan.call_count, 1)

    @patch('camadmiral.camera_identity.scan_explicit_address')
    def test_ip_or_stale_arp_alone_never_establishes_identity(self, scan):
        self.inventory.write_text(json.dumps({'devices': [device()]}))
        for observations in ([], [{'ip': '192.168.50.10'}], [device(), device()],
                             [{**device(), 'identity_conflict': True}]):
            scan.return_value = {'devices': observations}
            self.assertIsNone(discover_identity('192.168.50.10', self.inventory))
        scan.reset_mock()
        self.assertIsNone(discover_identity('127.0.0.1', self.inventory))
        scan.assert_not_called()

    def test_known_uuid_rejects_recycled_mac_and_ambiguous_candidates(self):
        adoption = self.adopt()
        expected = device()
        self.repository.bind_camera_identity(adoption['camera_uuid'], expected)
        links = self.repository.camera_identity_links()
        foreign = device(endpoint='urn:uuid:synthetic-foreign')
        self.assertFalse(same_identity(expected, foreign))
        for wrong in (foreign, {**expected, 'identity_conflict': True}):
            with self.assertRaises(ValueError):
                self.repository.bind_camera_identity(adoption['camera_uuid'], wrong)
        duplicate = {**device('192.168.50.11'), 'candidate_uuid': 'duplicate'}
        self.assertIsNone(linked_candidate(adoption, {'one': expected, 'two': duplicate}, links))
        self.assertIsNone(linked_candidate(adoption, {'one': {**expected, 'identity_conflict': True}}, links))
        self.assertEqual(linked_candidate(adoption, {'foreign': foreign}, links)['ip'], expected['ip'])

    @patch('camadmiral.recovery.replace_streams')
    @patch('camadmiral.recovery.probe_source')
    @patch('camadmiral.recovery.probe_streams')
    def test_shared_physical_device_recovers_independent_channels_and_persists_link(self, relay, probe, replace):
        cameras = [self.adopt('channel-one'), self.adopt('channel-two')]
        for camera in cameras:
            self.repository.bind_camera_identity(camera['camera_uuid'], device())
        moved = device('192.168.50.15')
        foreign = {**device(endpoint='urn:uuid:synthetic-foreign'), 'candidate_uuid': 'foreign-device'}
        self.inventory.write_text(json.dumps({'devices': [moved, foreign]}))
        probe.return_value = ProbeResult('ready', 10)
        relay.side_effect = lambda sources, *_: {s['stream_uuid']: ProbeResult('ready', 10) for s in sources}
        outcomes = recover_inventory_addresses(self.repository, self.inventory)
        self.assertEqual([r.status for r in outcomes], ['recovered', 'recovered'])
        for before in cameras:
            after = self.repository.adoption_for_candidate(before['candidate_uuid'])
            self.assertEqual(before['camera_uuid'], after['camera_uuid'])
            self.assertEqual(before['roles'], after['roles'])
            self.assertEqual(before['streams'][0]['stream_uuid'], after['streams'][0]['stream_uuid'])
            self.assertEqual(after['streams'][0]['uri'], before['streams'][0]['uri'].replace('.10:', '.15:'))
            self.assertEqual(self.repository.camera_identity_links()[before['camera_uuid']]['ip'], moved['ip'])
            self.assertEqual(len(self.repository.camera_identity_history(before['camera_uuid'])), 2)
        replace.assert_called_once()
        self.assertEqual(len(replace.call_args.args[0]), 2)

    @patch('camadmiral.recovery.replace_streams')
    @patch('camadmiral.recovery.probe_source')
    def test_failed_validation_preserves_sources_ids_and_identity_history(self, probe, replace):
        before = self.adopt()
        self.repository.bind_camera_identity(before['camera_uuid'], device())
        history = self.repository.camera_identity_history(before['camera_uuid'])
        links = self.repository.camera_identity_links()
        self.inventory.write_text(json.dumps({'devices': [device('192.168.50.15')]}))
        probe.return_value = ProbeResult('unavailable', 10)
        self.assertEqual(recover_inventory_addresses(self.repository, self.inventory)[0].status, 'unavailable')
        self.assertEqual(self.repository.adoption_for_candidate(before['candidate_uuid']), before)
        self.assertEqual(self.repository.camera_identity_links(), links)
        self.assertEqual(self.repository.camera_identity_history(before['camera_uuid']), history)
        replace.assert_not_called()

    @patch('camadmiral.recovery.replace_streams')
    @patch('camadmiral.recovery.probe_source', return_value=ProbeResult('ready', 10))
    @patch('camadmiral.recovery.probe_streams', return_value={})
    def test_failed_relay_validation_rolls_back_without_advancing_physical_identity(self, _relay, _probe, replace):
        before = self.adopt()
        self.repository.bind_camera_identity(before['camera_uuid'], device())
        links = self.repository.camera_identity_links()
        history = self.repository.camera_identity_history(before['camera_uuid'])
        self.inventory.write_text(json.dumps({'devices': [device('192.168.50.15')]}))
        self.assertEqual(recover_inventory_addresses(self.repository, self.inventory)[0].status, 'runtime_failed')
        after = self.repository.adoption_for_candidate(before['candidate_uuid'])
        self.assertEqual(after['streams'][0]['uri'], before['streams'][0]['uri'])
        self.assertEqual(after['roles'], before['roles'])
        self.assertEqual(self.repository.camera_identity_links(), links)
        self.assertEqual(self.repository.camera_identity_history(before['camera_uuid']), history)
        self.assertEqual(replace.call_count, 2)

    @patch('camadmiral.camera_identity.scan_explicit_address')
    def test_unique_live_mac_without_onvif_can_be_learned_and_survives_reopen(self, scan):
        camera = self.adopt()
        scan.return_value = {'devices': [{**device(), 'onvif': None, 'uri': 'rtsp://viewer:synthetic-secret@192.168.50.10/live'}]}
        self.assertTrue(learn_import_identity(self.repository, camera['camera_uuid'], self.inventory))
        reopened = CameraRepository(self.root / 'database.db', b'k' * 32)
        reopened.migrate()
        saved = reopened.camera_identity_links()[camera['camera_uuid']]
        self.assertEqual(saved['mac'], '02:00:00:00:50:10')
        self.assertNotIn('uri', saved)
        self.assertNotIn('synthetic-secret', json.dumps(saved))

    @patch('camadmiral.camera_identity.discover_identity')
    def test_upgrade_needs_prior_identity_and_does_not_guess_from_working_ip(self, discover):
        before = self.adopt()
        self.repository.record_probe_results({before['streams'][0]['stream_uuid']: ProbeResult('ready', 10)})
        enrich_existing_imports(self.repository, self.inventory)
        discover.assert_not_called()
        self.repository.observe_camera_identity(before['camera_uuid'], device())
        discover.return_value = device()
        enrich_existing_imports(self.repository, self.inventory)
        self.assertIn(before['camera_uuid'], self.repository.camera_identity_links())

    @patch('camadmiral.recovery.probe_source')
    def test_disabled_import_stays_unchanged(self, probe):
        camera = self.adopt()
        self.repository.bind_camera_identity(camera['camera_uuid'], device())
        with self.repository.connect() as db:
            db.execute('UPDATE cameras SET enabled=0 WHERE camera_uuid=?', (camera['camera_uuid'],))
            db.commit()
        self.inventory.write_text(json.dumps({'devices': [device('192.168.50.15')]}))
        self.assertEqual(recover_inventory_addresses(self.repository, self.inventory), [])
        probe.assert_not_called()

    def test_targeted_scan_uses_physical_id_once_for_both_logical_channels(self):
        for channel in ('one', 'two'):
            camera = self.adopt(channel)
            self.repository.bind_camera_identity(camera['camera_uuid'], device())
            with self.repository.connect() as db:
                db.execute("UPDATE managed_streams SET health_status='offline' WHERE camera_uuid=?", (camera['camera_uuid'],))
                db.commit()
        request = self.root / 'scan-request.json'
        app_module.RECOVERY_SCAN_ATTEMPTS.clear()
        with (patch.object(app_module, 'INVENTORY', self.root / 'missing-inventory.json'),
              patch.object(app_module, 'SCAN_REQUEST', request),
              patch.object(app_module, '_read_scan_state', return_value={'status': 'complete'}),
              patch.object(app_module, '_selected_discovery_subnets', return_value=['192.168.50.0/24'])):
            self.assertTrue(app_module._queue_targeted_recovery_scan(self.repository))
        targets = json.loads(request.read_text())['targets']
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]['candidate_uuid'], 'physical-device')
        self.assertEqual(len(app_module.RECOVERY_SCAN_ATTEMPTS), 2)
        app_module.RECOVERY_SCAN_ATTEMPTS.clear()

    def test_confirmed_identity_recovery_waits_for_busy_media_and_notifies_readers(self):
        camera = self.adopt()
        lock = threading.Lock()
        entered = threading.Event()
        def recover(*args):
            self.assertTrue(lock.locked())
            entered.set()
            return [SimpleNamespace(status="recovered")]
        with patch.object(app_module, "_repository", return_value=self.repository), \
             patch.object(app_module, "discover_identity", return_value=device("192.168.50.15")), \
             patch.object(app_module.threading, "Thread") as dispatch, \
             patch.object(app_module, "MEDIA_LOCK", lock), \
             patch.object(app_module, "recover_inventory_addresses", side_effect=recover), \
             patch.object(app_module, "_queue_frigate_reconciliation") as notify:
            app_module.link_camera_identity(camera["camera_uuid"],
                app_module.CameraIdentityRequest(address="192.168.50.15", confirm=True,
                                                endpoint_reference="urn:uuid:synthetic-device"),
                "link-camera-identity")
            target = dispatch.call_args.kwargs["target"]
        # Restore Thread before starting the captured background callback.
        with patch.object(app_module, "MEDIA_LOCK", lock), \
             patch.object(app_module, "recover_inventory_addresses", side_effect=recover), \
             patch.object(app_module, "_queue_frigate_reconciliation") as notify:
            lock.acquire()
            worker = threading.Thread(target=target)
            worker.start()
            try:
                self.assertFalse(entered.wait(0.1))
            finally:
                lock.release()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(entered.is_set(), "Confirmed recovery was dropped while media was busy")
            notify.assert_called_once_with(wait=True)

    def test_confirmed_identity_failed_source_recovery_does_not_notify_readers(self):
        camera = self.adopt()
        with patch.object(app_module, "_repository", return_value=self.repository), \
             patch.object(app_module, "discover_identity", return_value=device("192.168.50.15")), \
             patch.object(app_module.threading, "Thread") as dispatch:
            app_module.link_camera_identity(camera["camera_uuid"],
                app_module.CameraIdentityRequest(address="192.168.50.15", confirm=True,
                                                endpoint_reference="urn:uuid:synthetic-device"),
                "link-camera-identity")
            target = dispatch.call_args.kwargs["target"]
        with patch.object(app_module, "MEDIA_LOCK", threading.Lock()), \
             patch.object(app_module, "recover_inventory_addresses", return_value=[SimpleNamespace(status="runtime_failed")]), \
             patch.object(app_module, "_queue_frigate_reconciliation") as notify:
            target()
            notify.assert_not_called()

    @patch.object(app_module, 'discover_identity')
    def test_identification_requires_review_and_rechecks_device_before_binding(self, discover):
        camera = self.adopt()
        discover.return_value = device('192.168.50.15')
        with patch.object(app_module, '_repository', return_value=self.repository):
            with self.assertRaises(app_module.HTTPException):
                app_module.link_camera_identity(camera['camera_uuid'], app_module.CameraIdentityRequest(address='192.168.50.15'))
            response = app_module.link_camera_identity(camera['camera_uuid'],
                app_module.CameraIdentityRequest(address='192.168.50.15'), 'link-camera-identity')
            self.assertEqual(json.loads(response.body)['status'], 'review')
            self.assertEqual(self.repository.camera_identity_links(), {})
            response = app_module.link_camera_identity(camera['camera_uuid'],
                app_module.CameraIdentityRequest(address='192.168.50.15', confirm=True, endpoint_reference='urn:uuid:changed'),
                'link-camera-identity')
            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.repository.camera_identity_links(), {})
            with patch.object(app_module.threading, 'Thread'):
                response = app_module.link_camera_identity(camera['camera_uuid'],
                    app_module.CameraIdentityRequest(address='192.168.50.15', confirm=True, endpoint_reference='urn:uuid:synthetic-device'),
                    'link-camera-identity')
            self.assertEqual(json.loads(response.body)['status'], 'linked')
            self.assertEqual(self.repository.adoption_for_candidate(camera['candidate_uuid']), camera)

    def test_ui_projection_retains_two_logical_channels(self):
        cameras = [self.adopt('one'), self.adopt('two')]
        for camera in cameras:
            self.repository.bind_camera_identity(camera['camera_uuid'], device())
        with (patch.object(app_module, '_repository', return_value=self.repository),
              patch.object(app_module, 'load_frigate_targets', return_value=[]),
              patch.object(app_module.RELAY_HEALTH_MONITOR, 'cached_frame', return_value=None)):
            state = app_module._decorate_adoptions({'devices': [device()], 'summary': {}})
        self.assertEqual({d['adoption']['camera_uuid'] for d in state['devices']}, {c['camera_uuid'] for c in cameras})
        self.assertEqual(len(state['devices']), 2)


if __name__ == '__main__':
    unittest.main()
