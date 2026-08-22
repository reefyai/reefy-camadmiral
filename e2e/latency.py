from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = [
    "docker",
    "compose",
    "--project-name",
    "camadmiral-latency",
    "--file",
    str(ROOT / "e2e" / "compose.yaml"),
]


def run(*arguments: str) -> None:
    subprocess.run([*COMPOSE, *arguments], cwd=ROOT, check=True)


def main() -> int:
    started = time.monotonic()
    try:
        run("down", "--volumes", "--remove-orphans")
        run("build", "camadmiral")
        run("up", "--detach", "camera-open", "latency-relay")
        run("run", "--rm", "test-driver", "relay-latency")
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"CamAdmiral relay latency benchmark failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            run("down", "--volumes", "--remove-orphans")
        except (OSError, subprocess.CalledProcessError):
            pass
    print(f"Relay latency benchmark completed in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
