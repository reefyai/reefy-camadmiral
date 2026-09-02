from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import TextIO

from .config import SecretConfigurationError, database_path, settings
from .crypto import load_master_key
from .storage import CameraRepository


@dataclass
class Child:
    name: str
    command: list[str]
    restartable: bool = False
    redact_logs: bool = False
    process: subprocess.Popen[str] | None = None
    restarts: int = 0

    def start(self) -> None:
        print(f"supervisor: starting {self.name}", flush=True)
        if not self.redact_logs:
            self.process = subprocess.Popen(self.command)
            return
        self.process = subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if self.process.stdout is not None:
            threading.Thread(
                target=_forward_redacted_output,
                args=(self.process.stdout,),
                daemon=True,
                name=f"{self.name}-log-redactor",
            ).start()


STOP = False

_URL_USERINFO = re.compile(r"(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s]+@")
_URL_QUERY_CREDENTIAL = re.compile(
    r"(?P<prefix>[?&](?:user(?:name)?|pass(?:word)?)=)[^&\s\"']+",
    re.IGNORECASE,
)


def redact_log_credentials(line: str) -> str:
    redacted = _URL_USERINFO.sub(r"\g<scheme>***@", line)
    return _URL_QUERY_CREDENTIAL.sub(r"\g<prefix>***", redacted)


def _forward_redacted_output(output: TextIO) -> None:
    for line in output:
        print(redact_log_credentials(line.rstrip("\r\n")), flush=True)


def render_go2rtc_config(template: str, rtsp_password: str | None) -> str:
    listen = "0.0.0.0:18554" if rtsp_password else "127.0.0.1:18554"
    password = rtsp_password or "media-access-not-configured"
    return template.replace("__CAMADMIRAL_RTSP_LISTEN__", listen).replace(
        "__CAMADMIRAL_RTSP_PASSWORD__", password
    )


def request_stop(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def notify_relay_restart(
    repository: CameraRepository | None,
    *,
    reason: str,
    camera_count: int,
) -> None:
    if repository is None:
        return
    try:
        repository.enqueue_relay_restart_notification(
            reason=reason,
            camera_count=camera_count,
        )
    except Exception as exc:
        print(
            "supervisor: could not queue go2rtc restart notification: "
            f"{type(exc).__name__}",
            flush=True,
        )


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
    repository: CameraRepository | None = None
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
            redact_logs=True,
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
                    if child.name == "go2rtc" and repository is not None:
                        notify_relay_restart(
                            repository,
                            reason="unexpected_process_exit",
                            camera_count=0,
                        )
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
