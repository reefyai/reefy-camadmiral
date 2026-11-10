"""Match Frigate's single-input defaults without weakening source ownership."""
import copy
import unittest
from unittest.mock import Mock
from camadmiral.frigate_import import _running_inputs_match


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


if __name__=='__main__':unittest.main()
