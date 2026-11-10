import json
from pathlib import Path
import tempfile
import unittest

from scripts.ci_tools import input_hashes,validate_prepared


class PreparedToolchainTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'e2e').mkdir()
        (self.root/'requirements.txt').write_text('synthetic-runtime==1\n')
        (self.root/'e2e/requirements.txt').write_text('synthetic-browser==1\n')
        self.cache=self.root/'toolcache'
        prefix=self.cache/'3.13.18'
        (prefix/'x64/bin').mkdir(parents=True)
        (prefix/'x64/bin/python').touch();(prefix/'x64.complete').touch()
        self.browser=self.root/'synthetic-browser';self.browser.touch()
        self.marker=self.root/'prepared.json'
        self.marker.write_text(json.dumps({'inputs':input_hashes(self.root),'python_version':'3.13.18',
            'browser_binary':str(self.browser),'source':'synthetic-qualified-source'}))

    def validate(self):return validate_prepared(self.root,self.marker,self.cache)

    def test_matching_inputs_and_complete_installed_tools_are_reused(self):
        self.assertTrue(self.validate())

    def test_missing_marker_keeps_legacy_bootstrap_available(self):
        self.marker.unlink();self.assertFalse(self.validate())

    def test_each_changed_dependency_input_invalidates_prepared_environment(self):
        for name in ('requirements.txt','e2e/requirements.txt'):
            file=self.root/name;before=file.read_text();file.write_text(before+'synthetic-change==2\n')
            with self.assertRaisesRegex(RuntimeError,'rebuild'):self.validate()
            file.write_text(before)

    def test_missing_browser_and_incomplete_python_fail_before_tests(self):
        self.browser.unlink()
        with self.assertRaisesRegex(RuntimeError,'browser'):self.validate()
        self.browser.touch();(self.cache/'3.13.18/x64.complete').unlink()
        with self.assertRaisesRegex(RuntimeError,'Python'):self.validate()

    def test_corrupt_marker_fails_closed(self):
        self.marker.write_text('{')
        with self.assertRaises(ValueError):self.validate()
