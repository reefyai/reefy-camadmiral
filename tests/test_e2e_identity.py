from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from e2e import scenarios


class IdentityConsumerTelemetryTests(unittest.TestCase):
    def test_transition_uses_first_confirmed_moved_frame_not_latest_status_time(self) -> None:
        original = [10.0] * 12
        moved = [110.0] * 12
        records = [
            self._record(1, 90.0, original),
            self._record(2, 100.0, original),
            self._record(3, 110.0, moved),
            self._record(4, 111.0, moved),
            self._record(5, 112.0, moved),
            self._record(6, 160.0, moved),
        ]

        with tempfile.TemporaryDirectory() as directory:
            telemetry = Path(directory) / "frames.jsonl"
            telemetry.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            with patch.object(
                scenarios,
                "IDENTITY_FRAME_TELEMETRY_PATH",
                telemetry,
            ):
                transition = scenarios._identity_moved_frame_transition(
                    outage_started_at=101.0,
                    session_id="session-a",
                    original_fingerprint=original,
                    moved_fingerprint=moved,
                    source_separation=100.0,
                )

        self.assertIsNotNone(transition)
        self.assertEqual(transition["first_moved_frame_at"], 110.0)
        self.assertEqual(transition["first_moved_frame_index"], 3)
        self.assertEqual(transition["last_original_frame_at"], 100.0)
        self.assertEqual(transition["maximum_frame_gap"], 10.0)

    def test_transition_requires_three_consecutive_moved_frames_in_same_session(self) -> None:
        original = [10.0] * 12
        moved = [110.0] * 12
        records = [
            self._record(1, 90.0, original),
            self._record(2, 100.0, original),
            self._record(3, 105.0, moved),
            self._record(4, 106.0, moved),
            self._record(5, 107.0, original),
            self._record(6, 108.0, moved, session_id="other-session"),
            self._record(7, 109.0, moved),
            self._record(8, 110.0, moved),
            self._record(9, 111.0, moved),
        ]

        with tempfile.TemporaryDirectory() as directory:
            telemetry = Path(directory) / "frames.jsonl"
            telemetry.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            with patch.object(
                scenarios,
                "IDENTITY_FRAME_TELEMETRY_PATH",
                telemetry,
            ):
                transition = scenarios._identity_moved_frame_transition(
                    outage_started_at=101.0,
                    session_id="session-a",
                    original_fingerprint=original,
                    moved_fingerprint=moved,
                    source_separation=100.0,
                )

        self.assertIsNotNone(transition)
        self.assertEqual(transition["first_moved_frame_at"], 109.0)
        self.assertEqual(transition["first_moved_frame_index"], 7)

    @staticmethod
    def _record(
        frame_index: int,
        decoded_at: float,
        fingerprint: list[float],
        *,
        session_id: str = "session-a",
    ) -> dict[str, object]:
        return {
            "session_id": session_id,
            "frame_index": frame_index,
            "decoded_at": decoded_at,
            "fingerprint": fingerprint,
        }


if __name__ == "__main__":
    unittest.main()
