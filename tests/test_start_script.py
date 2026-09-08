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
if [ "$1" = pull ] && [ "${DOCKER_PULL_FAIL:-0}" = 1 ]; then
    exit 1
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
        arguments: tuple[str, ...] = (),
        container_exists: bool = False,
        container_running: bool = True,
        extra_environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{self.bin}:{environment['PATH']}"
        environment["DOCKER_LOG"] = str(self.log)
        environment["DOCKER_CONTAINER_EXISTS"] = "1" if container_exists else "0"
        environment["DOCKER_CONTAINER_RUNNING"] = "true" if container_running else "false"
        environment.update(extra_environment or {})
        return subprocess.run(
            [str(script), *arguments],
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

    def test_update_pulls_then_replaces_running_container_and_preserves_volume(self) -> None:
        completed = self.run_script(
            self.start_script,
            arguments=("--update",),
            container_exists=True,
            container_running=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.log.read_text(encoding="utf-8").splitlines()
        pull = "pull ghcr.io/reefyai/reefy-camadmiral:latest"
        stop = "stop camadmiral"
        remove = "rm camadmiral"
        self.assertIn(pull, calls)
        self.assertIn(stop, calls)
        self.assertIn(remove, calls)
        self.assertLess(calls.index(pull), calls.index(stop))
        self.assertLess(calls.index(stop), calls.index(remove))
        self.assertTrue(any(call.startswith("run -d --name camadmiral") for call in calls))
        self.assertFalse(any(call.startswith("volume rm") for call in calls))
        self.assertIn("Persistent data and credentials were preserved", completed.stdout)

    def test_update_replaces_stopped_container_without_stopping_it_again(self) -> None:
        completed = self.run_script(
            self.start_script,
            arguments=("--update",),
            container_exists=True,
            container_running=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.log.read_text(encoding="utf-8")
        self.assertNotIn("stop camadmiral", calls)
        self.assertIn("rm camadmiral", calls)
        self.assertIn("run -d --name camadmiral", calls)

    def test_failed_update_pull_leaves_existing_container_untouched(self) -> None:
        completed = self.run_script(
            self.start_script,
            arguments=("--update",),
            container_exists=True,
            extra_environment={"DOCKER_PULL_FAIL": "1"},
        )

        self.assertNotEqual(completed.returncode, 0)
        calls = self.log.read_text(encoding="utf-8")
        self.assertIn("pull ghcr.io/reefyai/reefy-camadmiral:latest", calls)
        self.assertNotIn("stop camadmiral", calls)
        self.assertNotIn("rm camadmiral", calls)

    def test_update_with_custom_image_recreates_without_pulling(self) -> None:
        completed = self.run_script(
            self.start_script,
            arguments=("--update",),
            container_exists=True,
            extra_environment={"CAMADMIRAL_IMAGE": "camadmiral:local"},
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.log.read_text(encoding="utf-8")
        self.assertIn("image inspect camadmiral:local", calls)
        self.assertNotIn("pull ", calls)
        self.assertIn("rm camadmiral", calls)
        self.assertIn("camadmiral:local", calls)

    def test_rejects_unknown_arguments_without_calling_docker(self) -> None:
        completed = self.run_script(
            self.start_script,
            arguments=("--replace",),
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("Usage: ./start-camadmiral.sh [--update]", completed.stderr)
        self.assertFalse(self.log.exists())

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
