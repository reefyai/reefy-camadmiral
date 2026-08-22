from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from e2e.scenarios import recovered_streams_ready


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

    def test_tag_release_reuses_the_exact_tested_commit(self) -> None:
        publish = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        gate = (ROOT / ".github" / "workflows" / "release-gate.yml").read_text()

        self.assertIn('tags: ["v*"]', publish)
        self.assertIn("uses: actions/github-script@v7", publish)
        self.assertIn("run.head_sha === context.sha", publish)
        self.assertIn('workflow_id: "release-gate.yml"', publish)
        self.assertIn('status: "success"', publish)
        self.assertIn("needs: verify-release-gate", publish)
        self.assertIn("platforms: linux/amd64", publish)
        self.assertNotIn("linux/arm64", publish)
        self.assertNotIn("docker/setup-qemu-action", publish)
        self.assertNotIn("uses: ./.github/workflows/release-gate.yml", publish)

    def test_release_gate_uses_big_runner_only_for_trusted_code(self) -> None:
        gate = (ROOT / ".github" / "workflows" / "release-gate.yml").read_text()

        self.assertIn('"self-hosted","Linux","X64","big-bird","large"', gate)
        self.assertIn("github.event.pull_request.head.repo.full_name != github.repository", gate)
        self.assertIn('["ubuntu-latest"]', gate)
        self.assertNotIn('tags: ["v*"]', gate)

    def test_e2e_snapshot_wait_allows_bounded_recovery(self) -> None:
        scenarios = (ROOT / "e2e" / "scenarios.py").read_text()

        self.assertIn(
            'wait_for("valid camera snapshot", valid_snapshot, timeout=60, interval=1)',
            scenarios,
        )
        self.assertIn('raise ScenarioFailure(f"Snapshot returned HTTP {status}")', scenarios)

    def test_e2e_requires_periodic_cache_only_thumbnail(self) -> None:
        scenarios = (ROOT / "e2e" / "scenarios.py").read_text()

        self.assertIn(
            'wait_for("periodic cached camera thumbnail", valid_thumbnail, timeout=60, interval=1)',
            scenarios,
        )
        self.assertIn("/thumbnail.jpg", scenarios)

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

    def test_e2e_full_sync_preserves_operator_owned_frigate_resources(self) -> None:
        scenarios = (ROOT / "e2e" / "scenarios.py").read_text()
        fixture = (ROOT / "e2e" / "fixtures" / "frigate.yml").read_text()
        compose = (ROOT / "e2e" / "compose.yaml").read_text()
        self.assertIn('stale_camera = "camadmiral_synthetic_stale"', scenarios)
        self.assertIn('operator_camera = "operator_camera"', scenarios)
        self.assertIn("camadmiral_synthetic_stale_record:", fixture)
        self.assertIn("operator_stream:", fixture)
        self.assertIn('"X-CamAdmiral-Action": "full-sync-frigate-target"', scenarios)
        self.assertIn("Full sync removed an operator-owned Frigate camera", scenarios)
        self.assertIn("Full sync removed an operator-owned Frigate stream", scenarios)
        self.assertIn('"Frigate stale camera worker cleanup"', scenarios)
        self.assertIn("restart: unless-stopped", compose)
        self.assertIn("test -f /config/config.yml || cp", compose)

    def test_e2e_full_sync_covers_stream_missing_only_from_runtime(self) -> None:
        scenarios = (ROOT / "e2e" / "scenarios.py").read_text()
        self.assertIn('partial_drift_stream = "camadmiral_synthetic_partial_drift"', scenarios)
        self.assertIn("Could not seed partial Frigate runtime drift", scenarios)
        self.assertIn('method="DELETE"', scenarios)
        self.assertIn("Partial-drift Frigate stream remained in runtime", scenarios)
        self.assertIn("Full sync left the partial-drift stream in Frigate config", scenarios)

    def test_e2e_full_sync_covers_rejected_delete_after_live_removal(self) -> None:
        runner = (ROOT / "e2e" / "run.py").read_text()
        scenarios = (ROOT / "e2e" / "scenarios.py").read_text()

        self.assertIn("frigate-ambiguous-delete-setup", runner)
        self.assertIn("/config/go2rtc_homekit.yml", runner)
        self.assertIn("frigate-ambiguous-delete-verify", runner)
        self.assertIn("Ambiguous-delete full sync failed", scenarios)
        self.assertIn("ambiguous partial-success deletion recovered", scenarios)

    def test_e2e_checks_mobile_actions_in_webkit(self) -> None:
        compose = (ROOT / "e2e" / "compose.yaml").read_text()
        runner = (ROOT / "e2e" / "run.py").read_text()
        browser = (ROOT / "e2e" / "ui.py").read_text()
        gate = (ROOT / ".github" / "workflows" / "release-gate.yml").read_text()

        self.assertIn('"127.0.0.1::18080"', compose)
        self.assertIn("ui_scenario()", runner)
        self.assertIn('playwright.webkit.launch(headless=True)', browser)
        self.assertIn('viewport={"width": 390, "height": 844}', browser)
        self.assertIn('.evaluate_all(', browser)
        self.assertIn("bounds.bottom > cellBounds.bottom + 0.5", browser)
        self.assertIn("bounds.bottom > cardBounds.bottom + 0.5", browser)
        self.assertIn("assert_mobile_settings(page)", browser)
        self.assertIn('page.goto(f"{BASE_URL}/settings/notifications",', browser)
        self.assertIn('to_have_url(re.compile(r"/settings/integrations$"))', browser)
        self.assertIn('to_have_url(re.compile(r"/incidents$"))', browser)
        self.assertIn("Settings section extends beyond the mobile viewport", browser)
        self.assertIn("assert_downstream_password_masking(page)", browser)
        self.assertIn('":********@" not in displayed', browser)
        self.assertIn('urllib.parse.unquote(parsed.password or "") != access["password"]', browser)
        self.assertIn('has_text="rtsp://"', browser)
        self.assertIn('expect(copy_button).to_have_text("✓", timeout=5_000)', browser)
        self.assertIn("python -m playwright install --with-deps webkit", gate)

    def test_e2e_scans_every_connected_private_subnet(self) -> None:
        compose = (ROOT / "e2e" / "compose.yaml").read_text()
        runner = (ROOT / "e2e" / "run.py").read_text()
        scenarios = (ROOT / "e2e" / "scenarios.py").read_text()
        multi_subnet_scenario = scenarios.split("def multi_subnet_discovery()", 1)[1].split(
            "def load_state()", 1
        )[0]

        self.assertIn("172.31.0.87", compose)
        self.assertIn("gw_priority: 1", compose)
        self.assertIn('scenario("multi-subnet-discovery")', runner)
        multi_subnet_position = runner.index('scenario("multi-subnet-discovery")')
        reset_position = runner.index(
            'run("down", "--volumes", "--remove-orphans")',
            multi_subnet_position,
        )
        baseline_position = runner.index('scenario("baseline")')
        self.assertLess(multi_subnet_position, reset_position)
        self.assertLess(reset_position, baseline_position)
        self.assertIn(
            "manual discovery on a non-default connected subnet",
            multi_subnet_scenario,
        )
        self.assertIn("full discovery across every connected subnet", multi_subnet_scenario)
        self.assertIn("explicit_request = request_json(", multi_subnet_scenario)
        self.assertIn('state.get("scan_id") != explicit_scan_id', multi_subnet_scenario)
        self.assertIn('state.get("scan_id") != full_scan_id', multi_subnet_scenario)

    def test_address_recovery_accepts_idle_recording_stream(self) -> None:
        adoption = {
            "roles": {"detect": "detect-stream", "record": "record-stream"},
            "streams": [
                {
                    "stream_uuid": "detect-stream",
                    "uri": "rtsp://192.0.2.12/detect",
                    "health_status": "healthy",
                },
                {
                    "stream_uuid": "record-stream",
                    "uri": "rtsp://192.0.2.12/record",
                    "health_status": "unknown",
                },
            ],
        }

        self.assertTrue(recovered_streams_ready(adoption, "192.0.2.12"))
        adoption["streams"][0]["health_status"] = "unknown"
        self.assertFalse(recovered_streams_ready(adoption, "192.0.2.12"))
        adoption["streams"][0]["health_status"] = "healthy"
        adoption["streams"][1]["uri"] = "rtsp://192.0.2.10/record"
        self.assertFalse(recovered_streams_ready(adoption, "192.0.2.12"))


if __name__ == "__main__":
    unittest.main()
