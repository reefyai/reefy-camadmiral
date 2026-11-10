"""Match Frigate's single-input defaults without weakening source ownership."""
import copy
import unittest
from unittest.mock import Mock, patch
from camadmiral.frigate_import import _running_inputs_match, build_managed


class InputRolesTests(unittest.TestCase):
    def check(self, expected, actual):
        saved=copy.deepcopy(expected)
        client=Mock()
        client.raw_paths.return_value={'cameras':{'synthetic_camera':{'ffmpeg':{'inputs':actual}}}}
        result=_running_inputs_match(client,'synthetic_camera',expected)
        self.assertEqual(expected,saved,'Comparison mutated saved rollback configuration')
        return result

    def one(self, roles, path='rtsp://operator:synthetic-password@192.0.2.10/video'):
        return [{'path':path,'roles':roles}]

    def test_detect_only_single_input_matches_implicit_record(self):
        self.assertTrue(self.check(self.one(['detect']),self.one(['record','detect'])))

    def test_record_only_single_input_matches_implicit_detect(self):
        self.assertTrue(self.check(self.one(['record']),self.one(['record','detect'])))

    def test_role_order_is_not_semantic(self):
        self.assertTrue(self.check(self.one(['detect','record']),self.one(['record','detect'])))

    def test_audio_is_preserved_when_single_input_roles_are_expanded(self):
        self.assertTrue(self.check(self.one(['audio']),self.one(['record','detect','audio'])))
        self.assertFalse(self.check(self.one(['audio']),self.one(['record','detect'])))

    def test_changed_path_is_rejected(self):
        self.assertFalse(self.check(self.one(['detect']),self.one(['record','detect'],'rtsp://192.0.2.10/different')))

    def test_changed_credentials_are_rejected(self):
        self.assertFalse(self.check(self.one(['detect']),self.one(['record','detect'],'rtsp://operator:changed-password@192.0.2.10/video')))

    def test_unexpected_audio_role_is_rejected(self):
        self.assertFalse(self.check(self.one(['detect']),self.one(['record','detect','audio'])))

    def test_multiple_inputs_require_their_actual_role_assignment(self):
        a=self.one(['record'])+self.one(['detect'],'rtsp://192.0.2.11/sub')
        b=copy.deepcopy(a);b[0]['roles'].append('detect')
        self.assertFalse(self.check(a,b))

    def test_missing_input_is_rejected(self):
        a=self.one(['record'])+self.one(['detect'],'rtsp://192.0.2.11/sub')
        self.assertFalse(self.check(a,a[:1]))

    def test_multiple_input_role_order_is_not_semantic(self):
        a=self.one(['record','audio'])+self.one(['detect'],'rtsp://192.0.2.11/sub')
        b=copy.deepcopy(a);b[0]['roles'].reverse()
        self.assertTrue(self.check(a,b))


class ManagedSingleInputTests(unittest.TestCase):
    def build(self, inputs):
        original = {'ffmpeg': {'inputs': inputs}, 'record': {'enabled': True},
                    'motion': {'mask': '0,0,0.1,0,0.1,0.1'}}
        row = {'camera_uuid': 'synthetic-camera', 'aliases': {'record': 'record_alias', 'detect': 'detect_alias'},
               'live': {}, 'original_streams': {}, 'original': original}
        saved = copy.deepcopy(original)
        stream = {'stream_uuid': 'synthetic-stream', 'roles': ['record', 'detect']}
        camera = {'enabled': True, 'streams': [stream]}
        with patch('camadmiral.frigate_import._camera', return_value=camera), \
             patch('camadmiral.frigate_import.relay_url', return_value='rtsp://192.0.2.20/synthetic'):
            managed, streams = build_managed(None, row, '192.0.2.20')
        self.assertEqual(original, saved, 'Managed conversion mutated the rollback configuration')
        self.assertEqual(managed['motion'], saved['motion'])
        self.assertEqual(set(streams), {'record_alias', 'detect_alias'})
        return managed['ffmpeg']['inputs']

    def test_implicit_roles_become_explicit_managed_readers(self):
        for roles in (['detect'], ['record']):
            with self.subTest(roles=roles):
                inputs = self.build([{'path': 'rtsp://192.0.2.10/main', 'roles': roles,
                                      'input_args': 'preset-rtsp-restream'}])
                self.assertEqual(inputs, [
                    {'path': 'rtsp://127.0.0.1:8554/record_alias', 'roles': ['record'],
                     'input_args': 'preset-rtsp-restream'},
                    {'path': 'rtsp://127.0.0.1:8554/detect_alias', 'roles': ['detect'],
                     'input_args': 'preset-rtsp-restream'}])

    def test_audio_is_assigned_to_one_reader_and_tuning_is_preserved(self):
        inputs = self.build([{'path': 'rtsp://192.0.2.10/main', 'roles': ['detect', 'audio'],
                              'hwaccel_args': 'preset-vaapi'}])
        self.assertEqual([item['roles'] for item in inputs], [['record', 'audio'], ['detect']])
        self.assertTrue(all(item['hwaccel_args'] == 'preset-vaapi' for item in inputs))

    def test_multiple_inputs_retain_explicit_role_ownership(self):
        inputs = self.build([
            {'path': 'rtsp://192.0.2.10/main', 'roles': ['record'], 'input_args': 'record-tuning'},
            {'path': 'rtsp://192.0.2.10/sub', 'roles': ['detect'], 'input_args': 'detect-tuning'}])
        self.assertEqual([(item['roles'], item['input_args']) for item in inputs],
                         [(['record'], 'record-tuning'), (['detect'], 'detect-tuning')])


if __name__=='__main__':unittest.main()
