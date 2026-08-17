import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camadmiral import diagnostics


class ScannerStatusTests(unittest.TestCase):
    def test_missing_heartbeat_is_starting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "missing.json"
            with patch.object(diagnostics, "SCANNER_HEARTBEAT", heartbeat):
                self.assertEqual(diagnostics.scanner_status(now=100)["state"], "starting")

    def test_fresh_heartbeat_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "heartbeat.json"
            heartbeat.write_text(json.dumps({"unix_time": 95}), encoding="utf-8")
            with patch.object(diagnostics, "SCANNER_HEARTBEAT", heartbeat):
                state = diagnostics.scanner_status(now=100)
            self.assertEqual(state["state"], "healthy")

    def test_old_heartbeat_is_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "heartbeat.json"
            heartbeat.write_text(json.dumps({"unix_time": 80}), encoding="utf-8")
            with patch.object(diagnostics, "SCANNER_HEARTBEAT", heartbeat):
                state = diagnostics.scanner_status(now=100)
            self.assertEqual(state["state"], "degraded")


class CatalogStatusTests(unittest.TestCase):
    def test_snapshot_reports_bundled_catalog_revision(self) -> None:
        with patch.object(diagnostics, "load_catalog", return_value={"revision": "test-revision"}):
            self.assertEqual(diagnostics.snapshot()["catalog_revision"], "test-revision")

    def test_unreadable_catalog_is_reported_without_breaking_health(self) -> None:
        with patch.object(
            diagnostics,
            "load_catalog",
            side_effect=diagnostics.CatalogError("invalid"),
        ):
            self.assertEqual(diagnostics.catalog_revision(), "unavailable")


if __name__ == "__main__":
    unittest.main()
