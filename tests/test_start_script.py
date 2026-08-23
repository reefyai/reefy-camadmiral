from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
START_SCRIPT = ROOT / "start-camadmiral.sh"
STOP_SCRIPT = ROOT / "stop-camadmiral.sh"


class LauncherScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.start_script = self.root / START_SCRIPT.name
        shutil.copy2(START_SCRIPT, self.start_script)
        self.start_script.chmod(0o755)
        self.stop_script = self.root / STOP_SCRIPT.name
        shutil.copy2(STOP_SCRIPT, self.stop_script)
        self.stop_script.chmod(0o755)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "docker.log"
        docker = self.bin / "docker"
        docker.write_text(
            """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_LOG"
if [ "$1" = container ] && [ "$2" = inspect ]; then
    [ "${DOCKER_CONTAINER_EXISTS:-0}" = 1 ] || exit 1
    case "$*" in
        *State.Running*) printf '%s\n' "${DOCKER_CONTAINER_RUNNING:-true}" ;;
    esac
    exit 0
fi
if [ "$1" = exec ]; then
    case "$*" in
        *admin-password) printf '%s\\n' generated-admin-password ;;
        *) printf '%s\\n' generated-api-token ;;
    esac
fi
exit 0
""",
            encoding="utf-8",
        )
        docker.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_script(
        self,
        script: Path,
        *,
        container_exists: bool = False,
        container_running: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{self.bin}:{environment['PATH']}"
        environment["DOCKER_LOG"] = str(self.log)
        environment["DOCKER_CONTAINER_EXISTS"] = "1" if container_exists else "0"
        environment["DOCKER_CONTAINER_RUNNING"] = "true" if container_running else "false"
        return subprocess.run(
            [str(script)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_generates_secrets_and_runs_one_hardened_container(self) -> None:
        completed = self.run_script(self.start_script)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.log.read_text(encoding="utf-8")
        self.assertIn("pull ghcr.io/reefyai/reefy-camadmiral:latest", calls)
        self.assertIn("volume create camadmiral-data", calls)
        self.assertIn("run --rm --volume camadmiral-data:/var/lib/camadmiral", calls)
        self.assertIn("run -d --name camadmiral --network host", calls)
        self.assertIn("--read-only --cap-drop ALL", calls)
        self.assertIn("--security-opt no-new-privileges:true", calls)
        self.assertIn("CAMADMIRAL_CONFIG_FILE=/var/lib/camadmiral/standalone.yaml", calls)
        self.assertNotIn("type=bind", calls)
        self.assertNotIn("compose", calls)
        self.assertIn("CamAdmiral is running at http://127.0.0.1:18080", completed.stdout)
        self.assertIn("Username: admin", completed.stdout)
        self.assertIn("Password: generated-admin-password", completed.stdout)

    def test_existing_container_is_started_without_reinitializing_state(self) -> None:
        completed = self.run_script(self.start_script, container_exists=True)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.log.read_text(encoding="utf-8")
        self.assertIn("start camadmiral", calls)
        self.assertNotIn("pull ", calls)
        self.assertNotIn("volume create", calls)
        self.assertNotIn("run --rm", calls)

    def test_stop_preserves_the_container_and_data(self) -> None:
        completed = self.run_script(
            self.stop_script,
            container_exists=True,
            container_running=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.log.read_text(encoding="utf-8")
        self.assertIn("stop camadmiral", calls)
        self.assertNotIn("rm ", calls)
        self.assertIn("CamAdmiral is stopped.", completed.stdout)
        self.assertIn("camadmiral-data volume are preserved", completed.stdout)
        self.assertIn("./start-camadmiral.sh", completed.stdout)

    def test_stop_is_idempotent_when_already_stopped(self) -> None:
        completed = self.run_script(
            self.stop_script,
            container_exists=True,
            container_running=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.log.read_text(encoding="utf-8")
        self.assertNotIn("stop camadmiral", calls)
        self.assertIn("CamAdmiral is already stopped.", completed.stdout)

    def test_stop_is_safe_when_camadmiral_is_not_installed(self) -> None:
        completed = self.run_script(self.stop_script)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.log.read_text(encoding="utf-8")
        self.assertNotIn("stop camadmiral", calls)
        self.assertIn("CamAdmiral is not installed. Nothing to stop.", completed.stdout)


if __name__ == "__main__":
    unittest.main()
