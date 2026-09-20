import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from e2e.timing import Timings


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "timings.jsonl"
        self.timings = Timings(self.path)
        self.timings.reset()

    def test_records_elapsed_time_and_progress(self):
        with patch("e2e.timing.time.monotonic", side_effect=[10, 12.5]), patch("builtins.print") as output:
            with self.timings.measure("scenario", "synthetic"):
                pass
        self.assertEqual(json.loads(self.path.read_text()), {
            "kind": "scenario", "name": "synthetic", "seconds": 2.5, "status": "passed"})
        self.assertEqual(output.call_count, 2)
        self.assertTrue(all(call.kwargs["flush"] for call in output.call_args_list))

    def test_failure_is_recorded_and_propagated(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            with self.timings.measure("scenario", "failure"):
                raise RuntimeError("synthetic failure")
        self.assertEqual(json.loads(self.path.read_text())["status"], "failed")

    def test_reset_discards_previous_run(self):
        with self.timings.measure("command", "up"):
            pass
        self.timings.reset()
        self.assertEqual(self.path.read_text(), "")
        self.assertEqual(self.timings.records, [])

    def test_nested_timings_stay_separate(self):
        with patch("e2e.timing.time.monotonic", side_effect=[0, 1, 3, 4]):
            with self.timings.measure("scenario", "outer"):
                with self.timings.measure("command", "inner"):
                    pass
        summary = self.timings.summary()
        self.assertIn("| inner | 2.0 | passed |", summary)
        self.assertIn("| outer | 4.0 | passed |", summary)
        self.assertIn("durations must not be added together", summary)
