"""Capture structured command output while retaining test diagnostics."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence


def run_logged(command: Sequence[str], *, cwd: Path, transcript: Path, check: bool = True) -> str:
    completed = subprocess.run(command, cwd=cwd, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with transcript.open('a') as log:
        log.write(completed.stdout)
        log.write(completed.stderr)
    if check and completed.returncode:
        print((completed.stdout + completed.stderr)[-10000:], flush=True)
        completed.check_returncode()
    return completed.stdout.strip()
