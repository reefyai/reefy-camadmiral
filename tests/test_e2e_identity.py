from __future__ import annotations

import json
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))

from e2e import run as e2e_run
from e2e import faults
from e2e import scenarios


class IdentityConsumerTelemetryTests(unittest.TestCase):
    @patch.object(faults, "streams")
    def test_frigate_probe_tracks_role_bound_detect_stream(
        self,
        streams,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "camadmiral.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    "CREATE TABLE camera_identity_periods ("
                    "camera_uuid TEXT, onvif_identity TEXT, ended_at TEXT);"
                    "CREATE TABLE managed_streams ("
                    "stream_uuid TEXT, stream_key TEXT, camera_uuid TEXT);"
                    "CREATE TABLE consumer_bindings (stream_uuid TEXT, role TEXT);"
                    "INSERT INTO camera_identity_periods VALUES "
                    "('camera-stable', 'urn:uuid:synthetic-onvif-camera', NULL);"
                    "INSERT INTO managed_streams VALUES "
                    "('record-id', 'stream-record', 'camera-stable'),"
                    "('detect-id', 'stream-detect', 'camera-stable');"
                    "INSERT INTO consumer_bindings VALUES "
                    "('record-id', 'record'), ('detect-id', 'detect');"
                )
            streams.return_value = {
                "stream-record": {"consumers": []},
                "stream-detect": {
                    "consumers": [
                        {
                            "id": 42,
                            "remote_addr": "172.30.0.30:45678",
                            "user_agent": "FFmpeg Frigate/0.17",
                        }
                    ]
                },
            }
            output = io.StringIO()
            with patch.object(faults, "DATABASE_PATH", database), redirect_stdout(output):
                faults.frigate_identity_consumers()

        observed = json.loads(output.getvalue())
        self.assertEqual(observed["stream_key"], "stream-detect")
        self.assertEqual(observed["consumers"][0]["id"], 42)

    @patch.object(e2e_run.time, "sleep")
    @patch.object(
        e2e_run,
        "frigate_identity_consumers",
        side_effect=[RuntimeError("transient disconnect"), {"stream_key": "stream-stable"}],
    )
    def test_frigate_baseline_waits_for_transient_connection(
        self,
        frigate_identity_consumers,
        sleep,
    ) -> None:
        observed = e2e_run.wait_for_frigate_identity_consumers(timeout=1)

        self.assertEqual(observed["stream_key"], "stream-stable")
        self.assertEqual(frigate_identity_consumers.call_count, 2)
        sleep.assert_called_once_with(0.5)

    @patch.object(e2e_run.time, "sleep")
    @patch.object(e2e_run, "assert_frigate_consumers_reconnected")
    @patch.object(e2e_run, "frigate_identity_consumers")
    def test_frigate_recovery_waits_for_replaced_connection(
        self,
        frigate_identity_consumers,
        assert_reconnected,
        sleep,
    ) -> None:
        previous = {"stream_key": "stream-stable", "consumers": [{"id": 1}]}
        current = {"stream_key": "stream-stable", "consumers": [{"id": 2}]}
        frigate_identity_consumers.side_effect = [previous, current]
        assert_reconnected.side_effect = [
            RuntimeError("old connection is still present"),
            None,
        ]

        observed = e2e_run.wait_for_frigate_identity_consumers(
            previous=previous,
            timeout=1,
        )

        self.assertEqual(observed, current)
        self.assertEqual(assert_reconnected.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_transition_uses_first_confirmed_moved_frame_not_latest_status_time(self) -> None:
        original = [10.0] * 12
        moved = [110.0] * 12
        records = [
            self._record(1, 90.0, original, session_id="session-original"),
            self._record(2, 100.0, original, session_id="session-original"),
            self._exit("session-original", 101.5),
            self._start("session-moved", 102.0),
            self._record(1, 110.0, moved, session_id="session-moved"),
            self._record(2, 111.0, moved, session_id="session-moved"),
            self._record(3, 112.0, moved, session_id="session-moved"),
            self._record(4, 160.0, moved, session_id="session-moved"),
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
                    reconnect_not_before=101.25,
                    original_session_id="session-original",
                    moved_session_id="session-moved",
                    original_fingerprint=original,
                    moved_fingerprint=moved,
                    source_separation=100.0,
                )

        self.assertIsNotNone(transition)
        self.assertEqual(transition["first_moved_frame_at"], 110.0)
        self.assertEqual(transition["first_moved_frame_index"], 1)
        self.assertEqual(transition["last_original_frame_at"], 100.0)
        self.assertEqual(transition["original_session_exited_at"], 101.5)
        self.assertEqual(transition["moved_session_started_at"], 102.0)
        self.assertEqual(transition["maximum_frame_gap"], 10.0)

    def test_transition_requires_three_consecutive_moved_frames_in_same_session(self) -> None:
        original = [10.0] * 12
        moved = [110.0] * 12
        records = [
            self._record(1, 90.0, original, session_id="session-original"),
            self._record(2, 100.0, original, session_id="session-original"),
            self._exit("session-original", 101.5),
            self._start("session-moved", 102.0),
            self._record(1, 105.0, moved, session_id="session-moved"),
            self._record(2, 106.0, moved, session_id="session-moved"),
            self._record(3, 107.0, original, session_id="session-moved"),
            self._record(1, 108.0, moved, session_id="other-session"),
            self._record(4, 109.0, moved, session_id="session-moved"),
            self._record(5, 110.0, moved, session_id="session-moved"),
            self._record(6, 111.0, moved, session_id="session-moved"),
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
                    reconnect_not_before=101.25,
                    original_session_id="session-original",
                    moved_session_id="session-moved",
                    original_fingerprint=original,
                    moved_fingerprint=moved,
                    source_separation=100.0,
                )

        self.assertIsNotNone(transition)
        self.assertEqual(transition["first_moved_frame_at"], 109.0)
        self.assertEqual(transition["first_moved_frame_index"], 4)

    def test_transition_accepts_source_disconnect_before_recovery_checkpoint(self) -> None:
        original = [10.0] * 12
        moved = [110.0] * 12
        records = [
            self._record(1, 100.0, original, session_id="session-original"),
            self._exit("session-original", 101.0),
            self._start("session-moved", 102.0),
            self._record(1, 103.0, moved, session_id="session-moved"),
            self._record(2, 104.0, moved, session_id="session-moved"),
            self._record(3, 105.0, moved, session_id="session-moved"),
        ]

        with tempfile.TemporaryDirectory() as directory:
            telemetry = Path(directory) / "frames.jsonl"
            telemetry.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            with patch.object(scenarios, "IDENTITY_FRAME_TELEMETRY_PATH", telemetry):
                transition = scenarios._identity_moved_frame_transition(
                    outage_started_at=100.5,
                    reconnect_not_before=101.5,
                    original_session_id="session-original",
                    moved_session_id="session-moved",
                    original_fingerprint=original,
                    moved_fingerprint=moved,
                    source_separation=100.0,
                )

        self.assertIsNotNone(transition)

    def test_transition_rejects_moved_session_before_recovery_checkpoint(self) -> None:
        original = [10.0] * 12
        moved = [110.0] * 12
        records = [
            self._record(1, 100.0, original, session_id="session-original"),
            self._exit("session-original", 101.0),
            self._start("session-moved", 102.0),
            self._record(1, 103.0, moved, session_id="session-moved"),
            self._record(2, 104.0, moved, session_id="session-moved"),
            self._record(3, 105.0, moved, session_id="session-moved"),
        ]

        with tempfile.TemporaryDirectory() as directory:
            telemetry = Path(directory) / "frames.jsonl"
            telemetry.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            with patch.object(scenarios, "IDENTITY_FRAME_TELEMETRY_PATH", telemetry):
                transition = scenarios._identity_moved_frame_transition(
                    outage_started_at=100.5,
                    reconnect_not_before=102.5,
                    original_session_id="session-original",
                    moved_session_id="session-moved",
                    original_fingerprint=original,
                    moved_fingerprint=moved,
                    source_separation=100.0,
                )

        self.assertIsNone(transition)

    def test_transition_requires_explicit_session_exit_and_start_events(self) -> None:
        original = [10.0] * 12
        moved = [110.0] * 12
        records = [
            self._record(1, 100.0, original, session_id="session-original"),
            self._record(1, 103.0, moved, session_id="session-moved"),
            self._record(2, 104.0, moved, session_id="session-moved"),
            self._record(3, 105.0, moved, session_id="session-moved"),
        ]

        with tempfile.TemporaryDirectory() as directory:
            telemetry = Path(directory) / "frames.jsonl"
            telemetry.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            with patch.object(scenarios, "IDENTITY_FRAME_TELEMETRY_PATH", telemetry):
                transition = scenarios._identity_moved_frame_transition(
                    outage_started_at=100.5,
                    reconnect_not_before=101.5,
                    original_session_id="session-original",
                    moved_session_id="session-moved",
                    original_fingerprint=original,
                    moved_fingerprint=moved,
                    source_separation=100.0,
                )

        self.assertIsNone(transition)

    @staticmethod
    def _record(
        frame_index: int,
        decoded_at: float,
        fingerprint: list[float],
        *,
        session_id: str = "session-a",
    ) -> dict[str, object]:
        return {
            "event": "frame",
            "session_id": session_id,
            "frame_index": frame_index,
            "decoded_at": decoded_at,
            "fingerprint": fingerprint,
        }

    @staticmethod
    def _start(session_id: str, started_at: float) -> dict[str, object]:
        return {
            "event": "session_started",
            "session_id": session_id,
            "started_at": started_at,
        }

    @staticmethod
    def _exit(session_id: str, exited_at: float) -> dict[str, object]:
        return {
            "event": "session_exited",
            "session_id": session_id,
            "exited_at": exited_at,
        }


if __name__ == "__main__":
    unittest.main()
