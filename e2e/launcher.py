from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTAINER = "camadmiral"
VOLUME = "camadmiral-data"
URL = "http://127.0.0.1:18080"


def docker(*arguments: str, check: bool = True, capture: bool = False) -> str:
    completed = subprocess.run(
        ["docker", *arguments],
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
    )
    return completed.stdout.strip() if completed.stdout else ""


def exists(kind: str, name: str) -> bool:
    return subprocess.run(
        ["docker", kind, "inspect", name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def wait_for_health(password: str, timeout: float = 45) -> dict[str, object]:
    encoded = base64.b64encode(f"admin:{password}".encode()).decode()
    request = urllib.request.Request(
        f"{URL}/healthz",
        headers={"Authorization": f"Basic {encoded}"},
    )
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return json.loads(response.read())
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = str(exc)
            time.sleep(1)
    raise RuntimeError(f"Launcher URL did not become healthy: {last_error}")


def run_launcher() -> None:
    if exists("container", CONTAINER) or exists("volume", VOLUME):
        raise RuntimeError("Standalone launcher E2E resource already exists")
    try:
        with tempfile.TemporaryDirectory(prefix="camadmiral-launcher-") as temporary:
            directory = Path(temporary)
            script = directory / "start-camadmiral.sh"
            shutil.copy2(ROOT / "start-camadmiral.sh", script)
            script.chmod(0o755)

            first = subprocess.run(
                [str(script)],
                check=True,
                text=True,
                capture_output=True,
            )
            created_container = exists("container", CONTAINER)
            created_volume = exists("volume", VOLUME)
            if not created_container or not created_volume:
                raise RuntimeError("Launcher did not create its container and data volume")
            if URL not in first.stdout or "Username: admin" not in first.stdout:
                raise RuntimeError("Launcher did not print its URL and admin username")

            password_lines = [
                line.removeprefix("Password: ")
                for line in first.stdout.splitlines()
                if line.startswith("Password: ")
            ]
            password = password_lines[0] if len(password_lines) == 1 else ""
            if not password or f"Password: {password}" not in first.stdout:
                raise RuntimeError("Launcher did not print its generated admin password")
            health = wait_for_health(password)
            if not health.get("version"):
                raise RuntimeError("Launcher health response did not include an app version")

            container_before = docker("inspect", "--format", "{{.Id}}", CONTAINER, capture=True)
            second = subprocess.run(
                [str(script)],
                check=True,
                text=True,
                capture_output=True,
            )
            container_after = docker("inspect", "--format", "{{.Id}}", CONTAINER, capture=True)
            if container_before != container_after:
                raise RuntimeError("Launcher replaced its existing container")
            if f"Password: {password}" not in second.stdout:
                raise RuntimeError("Launcher replaced its existing admin password")

            host_config = json.loads(
                docker("inspect", "--format", "{{json .HostConfig}}", CONTAINER, capture=True)
            )
            if not host_config.get("ReadonlyRootfs"):
                raise RuntimeError("Launcher container root filesystem is writable")
            if host_config.get("NetworkMode") != "host":
                raise RuntimeError("Launcher container is not using host networking")
            if host_config.get("RestartPolicy", {}).get("Name") != "unless-stopped":
                raise RuntimeError("Launcher container restart policy is incorrect")
    finally:
        if exists("container", CONTAINER):
            docker("rm", "--force", CONTAINER, check=False)
        if exists("volume", VOLUME):
            docker("volume", "rm", "--force", VOLUME, check=False)
