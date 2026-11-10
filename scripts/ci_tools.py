"""Verify the immutable CI image matches the checkout's dependency inputs."""
from __future__ import annotations
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]
MARKER = Path('/opt/camadmiral-ci/prepared.json')


def input_hashes(root):
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ('requirements.txt', 'e2e/requirements.txt')}


def validate_prepared(root=ROOT, marker=MARKER, toolcache=Path('/opt/camadmiral-toolcache/Python')):
    if not marker.exists():
        return False
    data = json.loads(marker.read_text())
    if data.get('inputs') != input_hashes(root):
        raise RuntimeError('Prepared CI dependencies differ from checkout; rebuild and pin the CI tools image')
    if not Path(data['browser_binary']).is_file():
        raise RuntimeError('Prepared CI browser binary is missing')
    prefix = toolcache / data['python_version']
    if not (prefix / 'x64.complete').is_file() or not (prefix / 'x64/bin/python').is_file():
        raise RuntimeError('Prepared CI Python tool cache is incomplete')
    print('Prepared CI inputs and installed tool cache verified: ' + data['source'], file=sys.stderr, flush=True)
    return True


def write_manifest():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as browser:
        binary = browser.webkit.executable_path
    MARKER.write_text(json.dumps({'inputs': input_hashes(ROOT), 'python_version': platform.python_version(),
        'playwright_version': importlib.metadata.version('playwright'), 'browser_binary': binary,
        'source': os.environ['CAMADMIRAL_CI_TOOLS_REVISION']}, indent=2) + '\n')


if __name__ == '__main__':
    if sys.argv[1:] == ['write']:
        write_manifest()
    else:
        validate_prepared()
