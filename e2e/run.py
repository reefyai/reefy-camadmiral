from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "e2e" / "compose.yaml"
COMPOSE = [
    "docker",
    "compose",
    "--project-name",
    "camadmiral-e2e",
    "--file",
    str(COMPOSE_FILE),
    "--profile",
    "moved",
    "--profile",
    "rotated",
]


def run(*arguments: str, capture: bool = False) -> str:
    completed = subprocess.run(
        [*COMPOSE, *arguments],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return completed.stdout.strip() if completed.stdout else ""


def scenario(name: str) -> None:
    run("run", "--rm", "test-driver", name)


def main() -> int:
    keep = os.environ.get("CAMADMIRAL_E2E_KEEP") == "1"
    started = time.monotonic()
    try:
        run("down", "--volumes", "--remove-orphans")
        run("build", "camadmiral")
        run(
            "up", "--detach", "camadmiral", "camera-open", "camera-auth",
            "camera-onvif",
        )
        scenario("baseline")
        run("up", "--detach", "frigate", "frigate-api-proxy")
        scenario("frigate")

        run("exec", "-T", "camadmiral", "python", "/e2e/faults.py", "delete-managed-stream")
        scenario("runtime-drift")

        container_before = run("ps", "--quiet", "camadmiral", capture=True)
        run("exec", "-T", "camadmiral", "sh", "-c", 'kill "$(pidof go2rtc)"')
        scenario("runtime-recovery")
        container_after = run("ps", "--quiet", "camadmiral", capture=True)
        if not container_before or container_before != container_after:
            raise RuntimeError("CamAdmiral container restarted during the go2rtc child fault")

        run("stop", "camera-open")
        scenario("camera-outage")
        run("start", "camera-open")
        scenario("camera-recovery")

        run("restart", "camadmiral")
        scenario("container-restart")

        run(
            "exec", "-T", "camadmiral", "cp",
            "/e2e/fixtures/inventory-invalid-address.json",
            "/var/lib/camadmiral/inventory.json",
        )
        scenario("invalid-address")

        run("stop", "camera-open")
        run("rm", "--force", "camera-open")
        run("up", "--detach", "camera-open-moved")
        run(
            "exec",
            "-T",
            "camadmiral",
            "cp",
            "/e2e/fixtures/inventory-moved.json",
            "/var/lib/camadmiral/inventory.json",
        )
        scenario("address-recovery")

        run("stop", "camera-auth")
        run("rm", "--force", "camera-auth")
        run("up", "--detach", "camera-auth-rotated")
        scenario("rotated-camera-ready")
        scenario("credential-repair")
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"CamAdmiral E2E failed: {exc}", file=sys.stderr)
        if keep:
            print("CAMADMIRAL_E2E_KEEP=1, leaving the isolated lab running", file=sys.stderr)
        return 1
    finally:
        if not keep:
            try:
                run("down", "--volumes", "--remove-orphans")
            except (OSError, subprocess.CalledProcessError):
                pass
    print(f"CamAdmiral E2E passed in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
