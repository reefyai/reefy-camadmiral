import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from scripts.ci_docker import client_wrapper, owned_data, owned_root, prepare, prepare_data, private_environment, private_mountpoints, stop, verified_limits


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

    def test_job_client_routes_without_changing_the_outer_runner_environment(self):
        root = prepare(self.environment)
        binary = self.root / 'synthetic-client'
        binary.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                          'print(json.dumps({"args":sys.argv[1:],"host":os.environ.get("DOCKER_HOST")}))\n')
        binary.chmod(0o755)
        wrapper = self.root / 'docker'
        wrapper.write_text(client_wrapper(root, binary))
        wrapper.chmod(0o755)
        environment = dict(os.environ, DOCKER_HOST='unix:///synthetic-parent.sock', DOCKER_CONTEXT='parent')
        result = subprocess.run([str(wrapper), 'info', '--format', 'synthetic format'],
                                env=environment, check=True, text=True, capture_output=True)
        observed = json.loads(result.stdout)
        self.assertEqual(observed['args'], ['--config', str(root / 'config'), '--host',
                         'unix://' + str(root / 'docker.sock'), 'info', '--format', 'synthetic format'])
        self.assertIsNone(observed['host'])
        self.assertEqual(environment['DOCKER_HOST'], 'unix:///synthetic-parent.sock')

    def test_wrapper_prefetch_retries_tls_without_repeating_lab_startup(self):
        root = prepare(self.environment)
        binary = self.root / 'synthetic-docker-download'
        state = self.root / 'transfer-state'
        binary.write_text("""#!/usr/bin/env python3
import json,pathlib,sys
state=pathlib.Path(""" + repr(str(state)) + """)
args=sys.argv[1:]
while args and args[0] in ('--config','--host'):args=args[2:]
if args[:2]==['compose','config']:
 print(json.dumps({'services':{'application':{'image':'synthetic/image:1'}}}));sys.exit(0)
if args[:2]==['image','inspect']:
 sys.exit(0 if state.with_name('verified').exists() else 1)
if args[:1]==['pull']:
 count=int(state.read_text())+1 if state.exists() else 1
 state.write_text(str(count))
 if count==1:
  print('local error: tls: bad record MAC',file=sys.stderr);sys.exit(1)
 state.with_name('verified').write_text('verified');sys.exit(0)
if args[:2]==['compose','up']:
 startup=state.with_name('startups')
 startup.write_text(str(int(startup.read_text())+1 if startup.exists() else 1))
 if state.with_name('verified').exists():sys.exit(0)
 print('local error: tls: bad record MAC',file=sys.stderr);sys.exit(1)
sys.exit(99)
""")
        binary.chmod(0o755)
        wrapper=self.root/'docker'
        wrapper.write_text(client_wrapper(root,binary));wrapper.chmod(0o755)
        result=subprocess.run([str(wrapper),'compose','up','-d','application'],
                              capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(state.read_text(),'2')
        self.assertEqual(state.with_name('startups').read_text(),'1')
        self.assertIn('CI image transfer retry',result.stdout)

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
        ) as command, patch('scripts.ci_docker.owned_data', return_value=root / 'data'):
            with self.assertRaises(RuntimeError):
                stop()
        command.assert_called_once()
        self.assertTrue(marker.exists())

    def test_job_storage_rejects_existing_or_foreign_data(self):
        root = prepare(self.environment)
        storage = self.root / 'synthetic-storage'
        storage.mkdir()
        prepare_data(root, storage, self.environment)
        self.assertEqual(owned_data(root, storage, self.environment), storage / 'data')
        with self.assertRaises(ValueError):
            prepare_data(root, storage, self.environment)
        with self.assertRaises(ValueError):
            owned_data(root, storage, dict(self.environment, GITHUB_JOB='different-job'))
        self.assertTrue((storage / 'owner.json').exists())

    def test_readiness_rejects_missing_or_weaker_resource_limits(self):
        self.assertEqual(verified_limits('33554432\n32\n'), {'memory_max': 33554432, 'pids_max': 32})
        for output in ('max\n32', '33554432\nmax', '33554432\n64', 'invalid'):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                verified_limits(output)

    def test_cleanup_detaches_nested_owned_mounts_and_preserves_parent_and_foreign_paths(self):
        root = Path('/synthetic temp/camadmiral-docker-job')
        data = Path('/synthetic-storage/data')
        owned = [root / 'exec/netns', root / 'exec/netns/namespace', data, data / 'overlay/merged']
        foreign = [Path('/'), root.parent, Path(str(root) + '-other/exec/netns'), data.parent,
                   Path('/synthetic-storage/data-other'), Path('/synthetic-foreign/netns')]
        def record(path):
            escaped = str(path).replace(' ', r'\040')
            return '1 0 0:1 / ' + escaped + ' rw - tmpfs tmpfs rw'
        mounts = private_mountpoints(root, data, '\n'.join(map(record, owned + foreign)))
        self.assertEqual(set(mounts), set(owned))
        self.assertEqual([len(path.parts) for path in mounts], sorted((len(path.parts) for path in owned), reverse=True))

    def test_docker_workloads_get_an_isolated_job_container_and_post_cleanup(self):
        repository = Path(__file__).resolve().parents[1]
        for name in ('release-gate.yml', 'stalled-snapshot.yml', 'memory-pressure.yml', 'relay-latency.yml'):
            jobs = yaml.safe_load((repository / '.github/workflows' / name).read_text())['jobs']
            for job_id, job in jobs.items():
                steps = job.get('steps', [])
                if not any('e2e/' in step.get('run', '') for step in steps):
                    continue
                with self.subTest(workflow=name, job=job_id):
                    self.assertEqual(job['container'], {'image': 'ubuntu:24.04', 'options': '--privileged --cgroupns=private',
                                     'volumes': ['/var/lib/camadmiral-ci-docker']})
                    self.assertEqual(job['env']['CAMADMIRAL_CI_JOB_CONTAINER'], '1')
                    self.assertEqual(job['env']['AGENT_TOOLSDIRECTORY'], '/opt/camadmiral-toolcache')
                    self.assertEqual(job['env']['PIP_CACHE_DIR'], '/opt/camadmiral-pipcache')
                    checkout = next(i for i, step in enumerate(steps) if step.get('uses') == 'actions/checkout@v4')
                    setup = next(i for i, step in enumerate(steps) if step.get('uses') == './.github/actions/docker-lab')
                    workload = next(i for i, step in enumerate(steps) if 'e2e/' in step.get('run', ''))
                    self.assertLess(checkout, setup)
                    self.assertLess(setup, workload)
        action = yaml.safe_load((repository / '.github/actions/docker-lab/action.yml').read_text())
        self.assertEqual(action['runs']['post'], 'stop.js')
        self.assertEqual(action['runs']['post-if'], 'always()')
