import json
import unittest
from unittest.mock import patch
from e2e import scenarios


class FullSyncWaitTests(unittest.TestCase):
    @staticmethod
    def response(status, payload):
        return status, json.dumps(payload).encode(), {}

    def test_transient_busy_preserves_the_successful_result(self):
        result = {"status": "synchronized", "removed_cameras": 1, "removed_streams": 2}
        with patch.object(scenarios, 'request', side_effect=[
            self.response(409, {"status": "sync_busy"}), self.response(200, result),
        ]) as request, patch.object(scenarios.time, 'sleep'), \
             patch.object(scenarios.time, 'monotonic', side_effect=[0, 1]):
            self.assertEqual(result, scenarios.full_sync_when_idle('synthetic-target'))
        self.assertEqual(2, request.call_count)
        self.assertEqual(request.call_args_list[0], request.call_args_list[1])

    def test_permanently_busy_sync_fails_within_the_budget(self):
        with patch.object(scenarios, 'request', return_value=self.response(409, {"status": "sync_busy"})) as request, \
             patch.object(scenarios.time, 'monotonic', side_effect=[0, 31]), \
             patch.object(scenarios.time, 'sleep') as sleep:
            with self.assertRaisesRegex(scenarios.ScenarioFailure, 'HTTP 409: sync_busy'):
                scenarios.full_sync_when_idle('synthetic-target')
        request.assert_called_once()
        sleep.assert_not_called()

    def test_other_errors_are_not_retried(self):
        for status, payload in [(409, {"status": "configuration_conflict"}),
                                (503, {"status": "target_unavailable"})]:
            with self.subTest(status=status), \
                 patch.object(scenarios, 'request', return_value=self.response(status, payload)) as request, \
                 patch.object(scenarios.time, 'sleep') as sleep:
                with self.assertRaises(scenarios.ScenarioFailure):
                    scenarios.full_sync_when_idle('synthetic-target')
                request.assert_called_once()
                sleep.assert_not_called()


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
