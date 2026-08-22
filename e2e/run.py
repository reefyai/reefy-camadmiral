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


def ui_scenario() -> None:
    published = run("port", "camadmiral", "18080", capture=True)
    if not published:
        raise RuntimeError("CamAdmiral E2E web port was not published")
    address = published.splitlines()[0].replace("0.0.0.0:", "127.0.0.1:")
    environment = dict(os.environ)
    environment.update(
        {
            "CAMADMIRAL_E2E_WEB_URL": f"http://{address}",
            "CAMADMIRAL_E2E_ADMIN_PASSWORD": "synthetic-e2e-admin-password",
        }
    )
    subprocess.run(
        [sys.executable, str(ROOT / "e2e" / "ui.py")],
        cwd=ROOT,
        env=environment,
        check=True,
    )


def main() -> int:
    keep = os.environ.get("CAMADMIRAL_E2E_KEEP") == "1"
    started = time.monotonic()
    try:
        run("down", "--volumes", "--remove-orphans")
        run("build", "camadmiral")
        run(
            "up", "--detach", "camadmiral", "camera-open", "camera-auth",
            "camera-onvif", "camera-secondary", "camera-onvif-large",
            "camera-rtsp-large",
        )
        scenario("multi-subnet-discovery")
        run(
            "exec", "-T", "camadmiral", "python", "-c",
            "import socket; socket.create_connection(('172.29.0.88', 554), 2).close()",
        )
        scenario("large-subnet-multicast-discovery")
        run("down", "--volumes", "--remove-orphans")
        run(
            "up", "--detach", "camadmiral", "camera-open", "camera-auth",
            "camera-onvif",
        )
        scenario("baseline")
        ui_scenario()
        run("up", "--detach", "frigate", "frigate-api-proxy")
        scenario("frigate")
        scenario("frigate-ambiguous-delete-setup")
        run(
            "exec", "-T", "frigate", "python3", "-c",
            "from pathlib import Path; import yaml; "
            "path=Path('/dev/shm/go2rtc.yaml'); data=yaml.safe_load(path.read_text()); "
            "data.setdefault('streams', {}).pop('camadmiral_synthetic_ambiguous_delete_detect', None); "
            "path.write_text(yaml.safe_dump(data, sort_keys=False))",
        )
        scenario("frigate-ambiguous-delete-verify")
        run("stop", "frigate-api-proxy", "frigate")

        run("exec", "-T", "camadmiral", "python", "/e2e/faults.py", "delete-managed-stream")
        scenario("runtime-drift")

        container_before = run("ps", "--quiet", "camadmiral", capture=True)
        run("exec", "-T", "camadmiral", "sh", "-c", 'kill "$(pidof go2rtc)"')
        scenario("runtime-recovery")
        container_after = run("ps", "--quiet", "camadmiral", capture=True)
        if not container_before or container_before != container_after:
            raise RuntimeError("CamAdmiral container restarted during the go2rtc child fault")

        run("stop", "camera-open")
        run("restart", "camadmiral")
        scenario("camera-outage")
        run(
            "exec", "-T", "camadmiral", "python", "/e2e/faults.py",
            "mark-open-camera-scan-offline",
        )
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
        scenario("moved-camera-ready")
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
        try:
            logs = run("logs", "--no-color", "--tail", "200", "camadmiral", capture=True)
        except (OSError, subprocess.CalledProcessError):
            logs = ""
        if logs:
            print("Recent CamAdmiral logs:", file=sys.stderr)
            print(logs, file=sys.stderr)
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
