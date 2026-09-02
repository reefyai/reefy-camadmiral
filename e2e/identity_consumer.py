from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

try:
    from .frame_fingerprint import (
        FRAME_HEIGHT,
        FRAME_SIZE,
        FRAME_WIDTH,
        frame_fingerprint,
        mean_fingerprint,
    )
except ImportError:
    from frame_fingerprint import (
        FRAME_HEIGHT,
        FRAME_SIZE,
        FRAME_WIDTH,
        frame_fingerprint,
        mean_fingerprint,
    )


CONFIG_PATH = Path(os.environ.get("IDENTITY_CONFIG_PATH", "/state/identity-recovery.json"))
STATUS_PATH = Path(
    os.environ.get("IDENTITY_STATUS_PATH", "/state/identity-consumer-status.json")
)
TELEMETRY_PATH = Path(
    os.environ.get("IDENTITY_TELEMETRY_PATH", "/state/identity-consumer-frames.jsonl")
)
USER_AGENT_PREFIX = os.environ.get(
    "IDENTITY_USER_AGENT_PREFIX", "CamAdmiral-E2E-Identity"
)


def write_status(payload: dict[str, object]) -> None:
    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(STATUS_PATH)


def read_exact(stream, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def main() -> int:
    configuration = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    url = str(configuration["consumer_url"])
    url_sha256 = hashlib.sha256(url.encode("utf-8")).hexdigest()
    wrapper_id = str(uuid.uuid4())
    container_pid = os.getpid()
    total_frames = 0
    attempt = 0
    TELEMETRY_PATH.unlink(missing_ok=True)
    with TELEMETRY_PATH.open("a", encoding="utf-8", buffering=1) as telemetry:
        while True:
            attempt += 1
            session_id = str(uuid.uuid4())
            user_agent = f"{USER_AGENT_PREFIX}/{session_id}"
            started_at = time.time()
            process = subprocess.Popen(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-fflags",
                    "nobuffer",
                    "-flags",
                    "low_delay",
                    "-rtsp_transport",
                    "tcp",
                    "-user_agent",
                    user_agent,
                    "-i",
                    url,
                    "-map",
                    "0:v:0",
                    "-an",
                    "-vf",
                    f"scale={FRAME_WIDTH}:{FRAME_HEIGHT},format=rgb24",
                    "-fps_mode",
                    "passthrough",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if process.stdout is None:
                raise RuntimeError("Reconnectable consumer has no decoded output")
            frames = 0
            last_fingerprint: list[float] | None = None
            last_frame_at: float | None = None
            recent_fingerprints: list[list[float]] = []
            telemetry.write(
                json.dumps(
                    {
                        "event": "session_started",
                        "session_id": session_id,
                        "attempt": attempt,
                        "consumer_pid": process.pid,
                        "started_at": started_at,
                        "url_sha256": url_sha256,
                    }
                )
                + "\n"
            )
            write_status(
                {
                    "status": "starting",
                    "wrapper_id": wrapper_id,
                    "url_sha256": url_sha256,
                    "attempt": attempt,
                    "session_id": session_id,
                    "user_agent": user_agent,
                    "consumer_pid": process.pid,
                    "container_pid": container_pid,
                    "started_at": started_at,
                    "frames": frames,
                    "total_frames": total_frames,
                    "fingerprint": None,
                    "last_frame_at": None,
                }
            )
            while True:
                frame = read_exact(process.stdout, FRAME_SIZE)
                if len(frame) != FRAME_SIZE:
                    break
                frames += 1
                total_frames += 1
                last_frame_at = time.time()
                current_fingerprint = frame_fingerprint(frame)
                telemetry.write(
                    json.dumps(
                        {
                            "event": "frame",
                            "session_id": session_id,
                            "frame_index": frames,
                            "total_frame_index": total_frames,
                            "decoded_at": last_frame_at,
                            "fingerprint": current_fingerprint,
                        }
                    )
                    + "\n"
                )
                recent_fingerprints.append(current_fingerprint)
                del recent_fingerprints[:-5]
                last_fingerprint = mean_fingerprint(recent_fingerprints)
                write_status(
                    {
                        "status": "running",
                        "wrapper_id": wrapper_id,
                        "url_sha256": url_sha256,
                        "attempt": attempt,
                        "session_id": session_id,
                        "user_agent": user_agent,
                        "consumer_pid": process.pid,
                        "container_pid": container_pid,
                        "started_at": started_at,
                        "frames": frames,
                        "total_frames": total_frames,
                        "fingerprint": last_fingerprint,
                        "last_frame_at": last_frame_at,
                    }
                )
            try:
                return_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    return_code = process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    return_code = process.wait(timeout=2)
            exited_at = time.time()
            telemetry.write(
                json.dumps(
                    {
                        "event": "session_exited",
                        "session_id": session_id,
                        "attempt": attempt,
                        "consumer_pid": process.pid,
                        "return_code": return_code,
                        "exited_at": exited_at,
                        "frames": frames,
                    }
                )
                + "\n"
            )
            write_status(
                {
                    "status": "reconnecting",
                    "wrapper_id": wrapper_id,
                    "url_sha256": url_sha256,
                    "attempt": attempt,
                    "session_id": session_id,
                    "user_agent": user_agent,
                    "consumer_pid": process.pid,
                    "container_pid": container_pid,
                    "started_at": started_at,
                    "frames": frames,
                    "total_frames": total_frames,
                    "fingerprint": last_fingerprint,
                    "return_code": return_code,
                    "last_frame_at": last_frame_at,
                    "exited_at": exited_at,
                }
            )
            time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
