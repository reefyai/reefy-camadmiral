from __future__ import annotations

import json
import os
import platform
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .rtsp_catalog import CatalogError, load_catalog
from .version import get_version

STARTED_MONOTONIC = time.monotonic()
STARTED_AT = datetime.now(timezone.utc)
SCANNER_HEARTBEAT = Path("/run/camadmiral/scanner-heartbeat.json")


def _component(state: str, detail: str) -> dict[str, str]:
    return {"state": state, "detail": detail}


def scanner_status(now: float | None = None) -> dict[str, str]:
    now = time.time() if now is None else now
    try:
        heartbeat = json.loads(SCANNER_HEARTBEAT.read_text(encoding="utf-8"))
        age = max(0.0, now - float(heartbeat["unix_time"]))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return _component("starting", "Waiting for the local worker heartbeat")
    if age > 10:
        return _component("degraded", f"Worker heartbeat is {age:.1f}s old")
    return _component("healthy", f"Local heartbeat received {age:.1f}s ago")


def go2rtc_status() -> dict[str, str]:
    base_url = os.environ.get("CAMADMIRAL_GO2RTC_URL", "http://127.0.0.1:1984")
    request = urllib.request.Request(
        f"{base_url}/api/streams",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=0.75) as response:
            if response.status != 200:
                return _component("degraded", f"Internal API returned HTTP {response.status}")
            json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return _component("starting", f"Internal API unavailable: {type(exc).__name__}")
    version = os.environ.get("CAMADMIRAL_GO2RTC_VERSION", "unknown")
    return _component("healthy", f"v{version}, API restricted to loopback")


def ffprobe_status() -> dict[str, str]:
    try:
        result = subprocess.run(
            ["ffprobe", "-version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=1,
        )
        first_line = result.stdout.splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return _component("degraded", "ffprobe is not available")
    version = first_line.removeprefix("ffprobe version ").split()[0]
    return _component("healthy", f"v{version} available for future media validation")


def storage_status() -> dict[str, str]:
    data_directory = settings().storage.database.parent
    if not data_directory.exists():
        return _component("degraded", "Application data directory is missing")
    if not os.access(data_directory, os.W_OK):
        return _component("degraded", "Application data directory is not writable")
    return _component("healthy", "Persistent application storage is writable")


def catalog_revision() -> str:
    try:
        return str(load_catalog()["revision"])
    except CatalogError:
        return "unavailable"


def snapshot() -> dict[str, Any]:
    components = {
        "control": _component("healthy", "FastAPI control process is responding"),
        "scanner": scanner_status(),
        "go2rtc": go2rtc_status(),
        "ffprobe": ffprobe_status(),
        "storage": storage_status(),
    }
    healthy = all(item["state"] == "healthy" for item in components.values())
    return {
        "status": "healthy" if healthy else "starting",
        "version": get_version(),
        "checkpoint": os.environ.get("CAMADMIRAL_CHECKPOINT", "discovery-preview"),
        "revision": os.environ.get("CAMADMIRAL_REVISION", "local"),
        "catalog_revision": catalog_revision(),
        "started_at": STARTED_AT.isoformat(),
        "uptime_seconds": round(time.monotonic() - STARTED_MONOTONIC, 1),
        "runtime": {
            "python": platform.python_version(),
            "architecture": platform.machine(),
            "pid": os.getpid(),
        },
        "components": components,
        "guardrails": ["Credentials encrypted at rest", "No unbounded stream-path guessing"],
    }
