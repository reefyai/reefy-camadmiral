from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r"^v\d{4}\.\d{2}\.\d{2}-\d{2}$")


def validate_icon() -> None:
    data = (ROOT / "reefy" / "icon.png").read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise SystemExit("Reefy icon must be a PNG")
    width, height, _depth, color_type, _compression, _filter, _interlace = (
        struct.unpack(">IIBBBBB", data[16:29])
    )
    if (width, height) != (512, 512):
        raise SystemExit("Reefy icon must be 512x512")
    if color_type != 2:
        raise SystemExit("Reefy icon must be opaque RGB without an alpha channel")

    offset = 8
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        chunk_type = data[offset + 4:offset + 8]
        if chunk_type == b"tRNS":
            raise SystemExit("Reefy icon must not contain transparency")
        offset += length + 12


def main() -> int:
    validate_icon()
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
    e2e_compose = (ROOT / "e2e" / "compose.yaml").read_text(encoding="utf-8")
    tmpfs = manifest.get("tmpfs") or []
    if not tmpfs:
        raise SystemExit("Reefy manifest must declare writable tmpfs mounts")
    for mount in tmpfs:
        rendered_mount = f"      - {mount}"
        if rendered_mount not in compose:
            raise SystemExit(f"compose.yaml is missing Reefy tmpfs mount: {mount}")
        if rendered_mount not in e2e_compose:
            raise SystemExit(f"e2e/compose.yaml is missing Reefy tmpfs mount: {mount}")
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
