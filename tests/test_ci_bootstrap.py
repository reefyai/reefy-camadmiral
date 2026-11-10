import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT=Path(__file__).resolve().parents[1]


class BootstrapProcessTests(unittest.TestCase):
    def test_precheckout_workflow_uses_the_same_codified_bootstrap(self):
        source=(ROOT/'scripts/ci_bootstrap.sh').read_text().strip()
        workflow=(ROOT/'.github/workflows/ci-tools.yml').read_text()
        self.assertIn(textwrap.indent(source,' '*10),workflow)

    def run_bootstrap(self, failure):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            apt=root/'apt-get'
            apt.write_text('#!'+sys.executable+'''\nimport json,os,pathlib,sys
root=pathlib.Path(os.environ['SYNTHETIC_APT_STATE'])
with (root/'calls').open('a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
if 'update' in sys.argv:print('Signed package index verified');sys.exit(0)
if '--no-download' in sys.argv:print('Configured verified packages');sys.exit(0)
if not (root/'failed').exists():
 (root/'failed').touch();print(os.environ['SYNTHETIC_APT_FAILURE']);sys.exit(100)
print('Package archives verified')
''')
            (root/'timeout').write_text('#!/bin/sh\nshift\nexec "$@"\n')
            (root/'sleep').write_text('#!/bin/sh\nexit 0\n')
            for p in (apt,root/'timeout',root/'sleep'):p.chmod(0o755)
            script=os.environ.get('CAMADMIRAL_CI_BOOTSTRAP_SCRIPT',str(ROOT/'scripts/ci_bootstrap.sh'))
            result=subprocess.run(['bash',script],env=dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH'],
                SYNTHETIC_APT_STATE=str(root),SYNTHETIC_APT_FAILURE=failure),capture_output=True,text=True,timeout=10)
            calls=[json.loads(line) for line in (root/'calls').read_text().splitlines()]
            return result,calls

    def test_transient_download_is_retried_before_exactly_one_configuration(self):
        result,calls=self.run_bootstrap('Failed to fetch https://packages.invalid/synthetic.deb: bad record mac')
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(2,sum('update' in args for args in calls))
        self.assertEqual(2,sum('--download-only' in args for args in calls))
        self.assertEqual(1,sum('--no-download' in args for args in calls))

    def test_certificate_error_is_not_retried_or_configured(self):
        result,calls=self.run_bootstrap('Failed to fetch: certificate verification failed')
        self.assertNotEqual(0,result.returncode)
        self.assertEqual(1,sum('update' in args for args in calls))
        self.assertFalse(any('--no-download' in args for args in calls))
