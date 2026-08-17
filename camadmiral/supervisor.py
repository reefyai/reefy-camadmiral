from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

from .config import SecretConfigurationError, database_path, settings
from .crypto import load_master_key
from .storage import CameraRepository


@dataclass
class Child:
    name: str
    command: list[str]
    restartable: bool = False
    process: subprocess.Popen[bytes] | None = None
    restarts: int = 0

    def start(self) -> None:
        print(f"supervisor: starting {self.name}", flush=True)
        self.process = subprocess.Popen(self.command)


STOP = False


def render_go2rtc_config(template: str, rtsp_password: str | None) -> str:
    listen = "0.0.0.0:18554" if rtsp_password else "127.0.0.1:18554"
    password = rtsp_password or "media-access-not-configured"
    return template.replace("__CAMADMIRAL_RTSP_LISTEN__", listen).replace(
        "__CAMADMIRAL_RTSP_PASSWORD__", password
    )


def request_stop(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def stop_children(children: list[Child]) -> None:
    for child in reversed(children):
        if child.process is not None and child.process.poll() is None:
            child.process.terminate()
    deadline = time.monotonic() + 5
    for child in reversed(children):
        process = child.process
        if process is None:
            continue
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> int:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    configuration = settings()
    host = configuration.server.listen
    port = str(configuration.server.port)
    runtime_go2rtc_config = "/run/camadmiral/go2rtc.yaml"
    template = open("/etc/camadmiral/go2rtc.yaml", encoding="utf-8").read()
    try:
        repository = CameraRepository(database_path(), load_master_key())
        repository.migrate()
        rtsp_password = repository.rtsp_access_password()
    except SecretConfigurationError:
        rtsp_password = None
    runtime_config = render_go2rtc_config(template, rtsp_password)
    with open(runtime_go2rtc_config, "w", encoding="utf-8") as config_file:
        config_file.write(runtime_config)
    os.chmod(runtime_go2rtc_config, 0o600)
    children = [
        Child(
            "go2rtc",
            ["/usr/local/bin/go2rtc", "-config", runtime_go2rtc_config],
            restartable=True,
        ),
        Child("scanner", [sys.executable, "-m", "camadmiral.scanner_worker"]),
        Child(
            "control",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "camadmiral.app:app",
                "--host",
                host,
                "--port",
                port,
                "--no-access-log",
            ],
        ),
    ]

    for child in children:
        child.start()

    exit_code = 0
    try:
        while not STOP:
            for child in children:
                process = child.process
                if process is None or process.poll() is None:
                    continue
                if child.restartable and child.restarts < 5:
                    child.restarts += 1
                    delay = min(8, 2 ** (child.restarts - 1))
                    print(
                        f"supervisor: {child.name} exited; restart "
                        f"{child.restarts}/5 in {delay}s",
                        flush=True,
                    )
                    time.sleep(delay)
                    child.start()
                    continue
                print(
                    f"supervisor: required child {child.name} exited "
                    f"with status {process.returncode}",
                    flush=True,
                )
                exit_code = process.returncode or 1
                return exit_code
            time.sleep(0.25)
    finally:
        stop_children(children)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
