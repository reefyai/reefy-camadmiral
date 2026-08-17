import unittest

from camadmiral.scan_state import preserve_inventory


class ScanStateTests(unittest.TestCase):
    def test_running_scan_uses_last_completed_inventory(self) -> None:
        state = {"status": "running", "phase": "scanning", "scan_id": "new"}
        inventory = {
            "scan_id": "previous",
            "devices": [{"candidate_uuid": "candidate-1", "status": "online"}],
            "summary": {"devices": 1, "online": 1, "offline": 0},
            "network": {"subnet": "192.168.1.0/24"},
            "duration_ms": 1200,
            "completed_at": "2026-01-01T00:00:01+00:00",
            "raw_log": ["test scan log"],
        }

        result = preserve_inventory(state, inventory)

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["inventory_scan_id"], "previous")
        self.assertEqual(len(result["devices"]), 1)
        self.assertEqual(result["raw_log"], ["test scan log"])

    def test_completed_result_is_not_replaced(self) -> None:
        state = {"status": "complete", "scan_id": "new", "devices": [{"candidate_uuid": "new"}]}

        result = preserve_inventory(state, {"scan_id": "old", "devices": []})

        self.assertIs(result, state)


if __name__ == "__main__":
    unittest.main()
