from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def test_release_metadata_is_consistent(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "validate-release.py")],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_tag_release_has_one_gate_owner(self) -> None:
        publish = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        gate = (ROOT / ".github" / "workflows" / "release-gate.yml").read_text()

        self.assertIn('tags: ["v*"]', publish)
        self.assertIn("uses: ./.github/workflows/release-gate.yml", publish)
        self.assertNotIn('tags: ["v*"]', gate)

    def test_e2e_snapshot_wait_allows_bounded_recovery(self) -> None:
        scenarios = (ROOT / "e2e" / "scenarios.py").read_text()

        self.assertIn(
            'wait_for("valid camera snapshot", valid_snapshot, timeout=60, interval=1)',
            scenarios,
        )
        self.assertIn('raise ScenarioFailure(f"Snapshot returned HTTP {status}")', scenarios)

    def test_e2e_recovery_retries_without_frigate_contention(self) -> None:
        compose = (ROOT / "e2e" / "compose.yaml").read_text()
        runner = (ROOT / "e2e" / "run.py").read_text()
        scenarios = (ROOT / "e2e" / "scenarios.py").read_text()

        self.assertIn('CAMADMIRAL_RECOVERY_RETRY_INTERVAL: "60"', compose)
        self.assertIn('run("stop", "frigate-api-proxy", "frigate")', runner)
        self.assertIn('run("logs", "--no-color", "--tail", "200", "camadmiral"', runner)
        self.assertIn(
            'wait_for("validated camera address promotion", moved, timeout=180)',
            scenarios,
        )
        self.assertIn("last observation=", scenarios)


if __name__ == "__main__":
    unittest.main()
