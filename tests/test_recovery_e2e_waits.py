import unittest
from e2e import scenarios


class RecoveryWaitTests(unittest.TestCase):
    def setUp(self):
        self.before = {"candidate_uuid": "synthetic", "scan_id": "baseline",
                       "outage_started_at": 100}
        self.scan = {"scan_id": "next", "inventory_scan_id": "completed-recovery",
                     "status": "running", "scanners": {"recovery": "running"},
                     "completed_at": "1970-01-01T00:01:50+00:00",
                     "devices": [{"candidate_uuid": "synthetic", "status": "offline"}],
                     "raw_log": ["RECOVERY: target candidate synthetic", "RECOVERY: complete in 1000ms"]}

    def test_completed_evidence_survives_next_scan_start(self):
        self.assertEqual(scenarios.completed_recovery_scan_id(self.scan, self.before), "completed-recovery")

    def test_incident_budget_covers_idle_stream_observations(self):
        # Initial periodic delay plus three 30s samples, scheduling and diagnostics.
        self.assertGreaterEqual(scenarios.IDENTITY_OFFLINE_INCIDENT_TIMEOUT, 60 + 3 * (30 + 20 + 8))

    def test_current_completed_scan_is_accepted(self):
        self.scan.update(status="complete", scan_id="completed-recovery")
        self.assertEqual(scenarios.completed_recovery_scan_id(self.scan, self.before), "completed-recovery")

    def test_stale_or_incomplete_or_other_target_evidence_is_rejected(self):
        for changes in [
            {"inventory_scan_id": "baseline"},
            {"completed_at": "1970-01-01T00:00:50+00:00"},
            {"completed_at": None},
            {"raw_log": ["RECOVERY: target candidate synthetic"]},
            {"raw_log": ["RECOVERY: target candidate synthetic-other", "RECOVERY: complete in 1000ms"]},
            {"devices": [{"candidate_uuid": "synthetic", "status": "online"}]},
        ]:
            with self.subTest(changes=changes):
                self.assertIsNone(scenarios.completed_recovery_scan_id({**self.scan, **changes}, self.before))
