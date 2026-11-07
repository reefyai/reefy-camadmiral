import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from e2e.command_output import run_logged


class StructuredCommandOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.transcript = self.root / 'transcript.log'

    def command(self, source, *, check=True):
        return run_logged([sys.executable, '-c', source], cwd=self.root,
                          transcript=self.transcript, check=check)

    def test_stderr_during_json_output_does_not_corrupt_stdout(self):
        output = self.command(
            "import os; os.write(1,b'{\\n'); "
            "os.write(2,b'synthetic probe diagnostic\\n'); "
            "os.write(1,b'\"streams\":[{\"width\":480,\"height\":270}]}\\n')"
        )
        self.assertEqual(json.loads(output), {'streams': [{'width': 480, 'height': 270}]})
        transcript = self.transcript.read_text()
        self.assertIn('synthetic probe diagnostic', transcript)
        self.assertIn('"streams"', transcript)

    def test_nonzero_exit_is_not_hidden_by_valid_json(self):
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.command("import sys; print('{}'); print('synthetic failure',file=sys.stderr); sys.exit(7)")
        self.assertEqual(error.exception.returncode, 7)
        self.assertEqual(error.exception.stdout.strip(), '{}')
        self.assertIn('synthetic failure', error.exception.stderr)
        self.assertIn('synthetic failure', self.transcript.read_text())

    def test_unchecked_failure_still_keeps_diagnostics_out_of_stdout(self):
        output = self.command(
            "import sys; print('{}'); print('synthetic refusal',file=sys.stderr); sys.exit(9)",
            check=False,
        )
        self.assertEqual(json.loads(output), {})
        self.assertIn('synthetic refusal', self.transcript.read_text())

    def test_invalid_stdout_is_not_filtered_into_a_passing_json_result(self):
        output = self.command("print('invalid stdout'); print('{}')")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(output)
