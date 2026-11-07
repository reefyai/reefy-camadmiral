"""Capture structured command output while retaining test diagnostics."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence


def run_logged(command: Sequence[str], *, cwd: Path, transcript: Path, check: bool = True) -> str:
    completed = subprocess.run(command, cwd=cwd, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    with transcript.open('a') as log:
        log.write(completed.stdout)
    if check and completed.returncode:
        print(completed.stdout[-10000:], flush=True)
        completed.check_returncode()
    return completed.stdout.strip()
