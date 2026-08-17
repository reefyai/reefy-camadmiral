from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r"^v\d{4}\.\d{2}\.\d{2}-\d{2}$")


def main() -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise SystemExit("VERSION must use vYYYY.MM.DD-NN")
    manifest = json.loads((ROOT / "reefy" / "app.json").read_text(encoding="utf-8"))
    expected_image = f"ghcr.io/reefyai/reefy-camadmiral:{version}"
    if manifest.get("version") != version or manifest.get("image") != expected_image:
        raise SystemExit("VERSION and reefy/app.json release metadata differ")
    releases = manifest.get("releases") or []
    if not releases or releases[0] != {"version": version, "image": expected_image}:
        raise SystemExit("The first Reefy release must match VERSION")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    if f"image: {expected_image}" not in compose:
        raise SystemExit("compose.yaml image does not match VERSION")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "compile-rtsp-catalog.py"), "--check"],
        cwd=ROOT,
        check=False,
    )
    if result.returncode:
        raise SystemExit("compiled RTSP catalog does not match its source")
    print(f"release metadata valid: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
