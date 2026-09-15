"""Keep the tested media envelope aligned with every shipped entry point."""
import json
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class ResourceLimitTests(unittest.TestCase):
    def test_snapshot_decoder_filter_and_encoder_threads_are_bounded(self):
        config = yaml.safe_load((ROOT / 'go2rtc.yaml').read_text())['ffmpeg']
        self.assertIn('-threads 1', config['global'])
        self.assertIn('-filter_threads 1', config['global'])
        self.assertIn('-filter_complex_threads 1', config['global'])
        self.assertEqual(config['mjpeg'], '-c:v mjpeg -threads:v 1')

    def test_shipped_and_e2e_memory_limits_match(self):
        manifest = json.loads((ROOT / 'reefy/app.json').read_text())
        self.assertEqual(manifest['mem_limit'], '512m')
        for path in ('compose.yaml', 'e2e/compose.yaml'):
            service = yaml.safe_load((ROOT / path).read_text())['services']['camadmiral']
            self.assertEqual(service['mem_limit'], manifest['mem_limit'], path)
            self.assertEqual(service['pids_limit'], manifest['pids_limit'], path)
        launcher = (ROOT / 'start-camadmiral.sh').read_text()
        self.assertEqual(re.search(r'--memory\s+(\S+)', launcher).group(1), manifest['mem_limit'])
        regression = yaml.safe_load((ROOT / 'e2e/memory-compose.yaml').read_text())
        self.assertEqual(regression['services']['camadmiral']['mem_limit'],
                         '${CAMADMIRAL_E2E_MEMORY_LIMIT:-512m}')

    def test_full_release_gate_runs_memory_regression(self):
        gate = (ROOT / '.github/workflows/release-gate.yml').read_text()
        self.assertIn('python3 e2e/memory_pressure.py', gate)
        self.assertIn('uses: ./.github/workflows/stalled-snapshot.yml', gate)
        workflow = (ROOT / '.github/workflows/stalled-snapshot.yml').read_text()
        self.assertIn('python3 e2e/snapshot_health.py', workflow)

    def test_snapshot_health_e2e_matches_production_deadline(self):
        import os
        from unittest.mock import patch
        import subprocess
        import sys
        with patch.dict(os.environ):
            os.environ.pop('CAMADMIRAL_SNAPSHOT_TIMEOUT', None)
            value = subprocess.check_output([sys.executable, '-c',
                'from camadmiral.media import SNAPSHOT_TIMEOUT; print(SNAPSHOT_TIMEOUT)'], text=True)
        timeout = float(value)
        self.assertEqual(timeout, 30)
        config = yaml.safe_load((ROOT / 'e2e/snapshot-health-compose.yaml').read_text())
        self.assertEqual(float(config['services']['camadmiral']['environment']['CAMADMIRAL_SNAPSHOT_TIMEOUT']), timeout)
