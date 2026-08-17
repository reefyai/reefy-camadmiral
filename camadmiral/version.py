"""Resolve the user-facing CamAdmiral version."""

from __future__ import annotations

import os
from pathlib import Path


VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
DEVELOPMENT_VERSION = "0.0.0-dev"


def get_version() -> str:
    """Prefer Reefy's release metadata, then the standalone image metadata."""
    injected = os.environ.get("REEFY_APP_VERSION") or os.environ.get("CAMADMIRAL_VERSION")
    if injected and injected.strip():
        return injected.strip()
    try:
        version = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return DEVELOPMENT_VERSION
    return version or DEVELOPMENT_VERSION
