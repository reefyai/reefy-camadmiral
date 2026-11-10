import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import ci_packages
from scripts.ci_docker import ensure_docker_tools


class PackageAcquisitionTests(unittest.TestCase):
    @patch('scripts.ci_packages.time.sleep')
    @patch('scripts.ci_packages.command')
    def test_tls_retry_refreshes_index_and_configures_packages_only_once(self, command, sleep):
        command.side_effect = [(100, 'Failed to fetch: bad record mac'), (0, 'signed index verified'),
                               (0, 'archives verified'), (0, 'configured')]
        ci_packages.apt_get(['install', '-y', 'synthetic-package'])
        self.assertEqual([x.args[0] for x in command.call_args_list], [
            ['install', '--download-only', '-y', 'synthetic-package'], ['update'],
            ['install', '--download-only', '-y', 'synthetic-package'],
            ['install', '--no-download', '-y', 'synthetic-package']])
        sleep.assert_called_once_with(5)

    @patch('scripts.ci_packages.time.sleep')
    @patch('scripts.ci_packages.command')
    def test_mirror_mismatch_and_package_404_require_verified_fresh_index(self, command, sleep):
        for error in ['Failed to fetch https://packages.invalid/synthetic.deb 404 Not Found',
                      'Hash Sum mismatch', 'File has unexpected size']:
            command.reset_mock();command.side_effect = [(100,error),(0,'verified index'),(0,'verified archives'),(0,'installed')]
            ci_packages.apt_get(['install','-y','synthetic-package'])
            self.assertEqual(['update'],command.call_args_list[1].args[0])

    @patch('scripts.ci_packages.command')
    def test_trust_access_disk_and_configuration_errors_fail_without_retry(self, command):
        for error in ['Failed to fetch: certificate verification failed', 'Failed to fetch: 403  Forbidden',
                      'Repository is not signed', 'NO_PUBKEY', 'Invalid signature', 'No space left on device',
                      'Unable to locate package synthetic', 'dpkg was interrupted']:
            command.reset_mock();command.return_value=(100,error)
            with self.assertRaises(subprocess.CalledProcessError):ci_packages.apt_get(['install','synthetic-package'])
            self.assertEqual(1,command.call_count,error)

    @patch('scripts.ci_packages.command')
    def test_configuration_failure_is_not_retried(self, command):
        command.side_effect=[(0,'archives verified'),(1,'synthetic postinst failure')]
        with self.assertRaises(subprocess.CalledProcessError):ci_packages.apt_get(['install','synthetic-package'])
        self.assertEqual(2,command.call_count)

    @patch('scripts.ci_packages.time.sleep')
    @patch('scripts.ci_packages.command',side_effect=TimeoutError)
    def test_acquisition_timeout_has_a_finite_retry_count(self, command, sleep):
        with self.assertRaises(subprocess.CalledProcessError):ci_packages.acquire(['update'])
        self.assertEqual(ci_packages.ATTEMPTS,command.call_count)
        self.assertEqual(ci_packages.ATTEMPTS-1,sleep.call_count)

    @patch('scripts.ci_packages.command')
    def test_total_deadline_is_not_reset(self, command):
        with self.assertRaises(TimeoutError):ci_packages.acquire(['update'],deadline=0)
        command.assert_not_called()

    def test_timeout_kills_owned_acquisition_process_and_returns_promptly(self):
        import sys,time
        with tempfile.TemporaryDirectory() as temp:
            binary=Path(temp)/'synthetic-apt'
            binary.write_text('#!'+sys.executable+'\nimport subprocess,sys,time\n'
                              'subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\ntime.sleep(60)\n')
            binary.chmod(0o755)
            started=time.monotonic()
            with patch.object(ci_packages,'APT',str(binary)),self.assertRaises(TimeoutError):
                ci_packages.command(['update'],.1)
            self.assertLess(time.monotonic()-started,5)


class InstalledDockerToolsTests(unittest.TestCase):
    @patch('scripts.ci_packages.apt_get')
    @patch('scripts.ci_docker.shutil.which',return_value='/synthetic/tool')
    @patch('scripts.ci_docker.subprocess.run')
    def test_preinstalled_clients_skip_acquisition_without_using_a_daemon(self, run, which, apt):
        run.return_value.returncode=0
        ensure_docker_tools()
        apt.assert_not_called()
        self.assertEqual([x.args[0] for x in run.call_args_list],
                         [['dockerd','--version'],['docker','compose','version'],['docker','buildx','version']])

    @patch('scripts.ci_packages.apt_get')
    @patch('scripts.ci_docker.shutil.which',return_value=None)
    def test_missing_toolchain_uses_bounded_package_acquisition(self, which, apt):
        ensure_docker_tools()
        self.assertEqual([x.args[0] for x in apt.call_args_list],
                         [['update'],['install','-y','--no-install-recommends','docker.io','docker-compose-v2','docker-buildx']])
