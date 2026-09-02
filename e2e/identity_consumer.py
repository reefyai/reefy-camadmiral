from __future__ import annotations

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


CONFIG_PATH = Path("/state/identity-recovery.json")
STATUS_PATH = Path("/state/identity-consumer-status.json")
TELEMETRY_PATH = Path("/state/identity-consumer-frames.jsonl")


def write_status(payload: dict[str, object]) -> None:
    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(STATUS_PATH)


def main() -> int:
    configuration = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    url = str(configuration["consumer_url"])
    session_id = str(uuid.uuid4())
    user_agent = f"CamAdmiral-E2E-Identity/{session_id}"
    started_at = time.time()
    TELEMETRY_PATH.unlink(missing_ok=True)
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
        raise RuntimeError("Long-lived consumer has no decoded output")
    frames = 0
    last_fingerprint: list[float] | None = None
    last_frame_at: float | None = None
    recent_fingerprints: list[list[float]] = []
    write_status(
        {
            "status": "starting",
            "session_id": session_id,
            "user_agent": user_agent,
            "consumer_pid": process.pid,
            "container_pid": os.getpid(),
            "started_at": started_at,
            "frames": frames,
            "fingerprint": last_fingerprint,
            "last_frame_at": None,
        }
    )
    with TELEMETRY_PATH.open("a", encoding="utf-8", buffering=1) as telemetry:
        while True:
            frame = process.stdout.read(FRAME_SIZE)
            if len(frame) != FRAME_SIZE:
                break
            frames += 1
            last_frame_at = time.time()
            current_fingerprint = frame_fingerprint(frame)
            telemetry.write(
                json.dumps(
                    {
                        "session_id": session_id,
                        "frame_index": frames,
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
                    "session_id": session_id,
                    "user_agent": user_agent,
                    "consumer_pid": process.pid,
                    "container_pid": os.getpid(),
                    "started_at": started_at,
                    "frames": frames,
                    "fingerprint": last_fingerprint,
                    "last_frame_at": last_frame_at,
                }
            )
    return_code = process.wait(timeout=5)
    write_status(
        {
            "status": "exited",
            "session_id": session_id,
            "user_agent": user_agent,
            "consumer_pid": process.pid,
            "container_pid": os.getpid(),
            "started_at": started_at,
            "frames": frames,
            "fingerprint": last_fingerprint,
            "return_code": return_code,
            "last_frame_at": last_frame_at,
        }
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
