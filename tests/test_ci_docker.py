import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from scripts.ci_docker import owned_root, prepare, private_environment, stop


class PrivateDockerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = {'RUNNER_TEMP': str(self.root), 'GITHUB_RUN_ID': 'synthetic-run',
                            'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_JOB': 'synthetic-job'}

    def test_private_context_does_not_inherit_host_credentials_or_endpoint(self):
        root = prepare(self.environment)
        environment = private_environment(root, dict(self.environment,
            DOCKER_HOST='tcp://synthetic-parent:2376', DOCKER_CONFIG='/synthetic-parent/config',
            DOCKER_CONTEXT='parent', DOCKER_TLS_VERIFY='1', DOCKER_CERT_PATH='/synthetic-parent/certs'))
        self.assertEqual(environment['DOCKER_HOST'], 'unix://' + str(root / 'docker.sock'))
        self.assertEqual(environment['DOCKER_CONFIG'], str(root / 'config'))
        self.assertNotIn('DOCKER_CONTEXT', environment)
        self.assertNotIn('DOCKER_TLS_VERIFY', environment)
        self.assertNotIn('DOCKER_CERT_PATH', environment)
        self.assertEqual(json.loads((root / 'config/config.json').read_text()), {'auths': {}})
        self.assertEqual((root / 'config/config.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(owned_root(str(root), self.environment), root)

    def test_different_jobs_get_distinct_daemon_state(self):
        first, second = prepare(self.environment), prepare(self.environment)
        self.assertNotEqual(first, second)
        self.assertNotEqual(private_environment(first, {})['DOCKER_HOST'],
                            private_environment(second, {})['DOCKER_HOST'])

    def test_cleanup_refuses_another_jobs_data_and_preserves_it(self):
        root = prepare(self.environment)
        sentinel = root / 'retained.txt'
        sentinel.write_text('synthetic retained data')
        with self.assertRaises(ValueError):
            owned_root(str(root), dict(self.environment, GITHUB_JOB='different-job'))
        self.assertEqual(sentinel.read_text(), 'synthetic retained data')

    def test_cleanup_refuses_foreign_paths_and_symlinks(self):
        root = prepare(self.environment)
        alias = self.root / 'camadmiral-docker-link'
        alias.symlink_to(root, target_is_directory=True)
        for path in (str(self.root), str(alias), '/'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                owned_root(path, self.environment)
        self.assertTrue((root / 'owner.json').exists())

    def test_cleanup_refuses_a_different_daemon_before_removing_any_resources(self):
        root = prepare(self.environment)
        marker = root / 'owner.json'
        metadata = json.loads(marker.read_text())
        metadata['daemon'] = 'synthetic-job-daemon-fingerprint'
        marker.write_text(json.dumps(metadata))
        with patch.dict('os.environ', dict(self.environment, STATE_docker_root=str(root))), patch(
            'scripts.ci_docker.command', return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps({'ID': 'synthetic-foreign-daemon'}))
        ) as command:
            with self.assertRaises(RuntimeError):
                stop()
        command.assert_called_once()
        self.assertTrue(marker.exists())

    def test_docker_workloads_get_an_isolated_job_container_and_post_cleanup(self):
        repository = Path(__file__).resolve().parents[1]
        for name in ('release-gate.yml', 'stalled-snapshot.yml', 'memory-pressure.yml', 'relay-latency.yml'):
            jobs = yaml.safe_load((repository / '.github/workflows' / name).read_text())['jobs']
            for job_id, job in jobs.items():
                steps = job.get('steps', [])
                if not any('e2e/' in step.get('run', '') for step in steps):
                    continue
                with self.subTest(workflow=name, job=job_id):
                    self.assertEqual(job['container'], {'image': 'ubuntu:24.04', 'options': '--privileged'})
                    self.assertEqual(job['env']['CAMADMIRAL_CI_JOB_CONTAINER'], '1')
                    checkout = next(i for i, step in enumerate(steps) if step.get('uses') == 'actions/checkout@v4')
                    setup = next(i for i, step in enumerate(steps) if step.get('uses') == './.github/actions/docker-lab')
                    workload = next(i for i, step in enumerate(steps) if 'e2e/' in step.get('run', ''))
                    self.assertLess(checkout, setup)
                    self.assertLess(setup, workload)
        action = yaml.safe_load((repository / '.github/actions/docker-lab/action.yml').read_text())
        self.assertEqual(action['runs']['post'], 'stop.js')
        self.assertEqual(action['runs']['post-if'], 'always()')
