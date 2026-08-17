#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from camadmiral.rtsp_catalog import load_catalog


SOURCE = ROOT / "catalog" / "sources" / "official.json"
TARGET = ROOT / "camadmiral" / "rtsp_catalog.json"


def compiled_bytes() -> bytes:
    payload = json.loads(SOURCE.read_text(encoding="utf-8"))
    payload["rules"] = sorted(payload["rules"], key=lambda rule: rule["id"])
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = compiled_bytes()
    if args.check:
        if not TARGET.exists() or TARGET.read_bytes() != content:
            raise SystemExit("RTSP catalog is stale; run scripts/compile-rtsp-catalog.py")
    else:
        TARGET.write_bytes(content)
    load_catalog(TARGET)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
