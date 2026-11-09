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
from e2e.frigate_identity import decoded_frame_hash, recording_clip_interval, recording_for_playback, recording_playback_interval, reader_recovery_ready, FRIGATE_CONTROL_FAULT_SCRIPT


class FrigateRecordingPlaybackTests(unittest.TestCase):
    def test_inclusive_frigate_query_selects_only_the_requested_recording(self):
        with sqlite3.connect(':memory:') as connection:
            connection.execute('CREATE TABLE recordings (id TEXT, start_time REAL, end_time REAL)')
            connection.executemany('INSERT INTO recordings VALUES (?,?,?)',
                [('previous-resolution', 0, 10), ('selected-resolution', 10, 20), ('next-resolution', 20, 30)])
            # Match the pinned Frigate clip API's inclusive selection contract.
            query = ('SELECT id FROM recordings WHERE start_time BETWEEN ? AND ? '
                     'OR end_time BETWEEN ? AND ? OR (? > start_time AND ? < end_time)')
            def selected(start, end):
                return {row[0] for row in connection.execute(query, (start, end, start, end, start, end))}
            self.assertEqual(selected(10, 20), {'previous-resolution', 'selected-resolution', 'next-resolution'})
            self.assertEqual(selected(*recording_clip_interval(10, 20)), {'selected-resolution'})

    def test_short_recordings_keep_a_positive_interior_playback_interval(self):
        for start, end in ((10, 10.01), (1000, 1000.1), (20, 30)):
            with self.subTest(start=start, end=end):
                first, last = recording_clip_interval(start, end)
                self.assertLess(start, first)
                self.assertLess(first, last)
                self.assertLess(last, end)

    def test_invalid_recording_intervals_are_rejected(self):
        for start, end in ((10, 10), (20, 10), (0, float('inf')), (float('nan'), 10)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                recording_clip_interval(start, end)

    def test_recovery_requires_a_complete_new_segment_for_each_camera(self):
        complete = ['new-complete', 'synthetic-camera', '/synthetic/new.mp4', 20, 30]
        rows = [['old-complete', 'synthetic-camera', '/synthetic/old.mp4', 0, 10], complete,
                ['new-fragment', 'synthetic-camera', '/synthetic/fragment.mp4', 40, 40.1],
                ['other-complete', 'other-camera', '/synthetic/other.mp4', 50, 60]]
        self.assertEqual(recording_for_playback(rows, 'synthetic-camera', 10), complete)
        self.assertIsNone(recording_for_playback(rows, 'synthetic-camera', 30))
        self.assertIsNone(recording_for_playback(rows[-1:], 'synthetic-camera', 10))

    def test_overlapping_recordings_do_not_contaminate_selected_source(self):
        selected = ["selected", "synthetic-camera", "/synthetic/selected.mp4", 10, 20]
        rows = [
            ["previous", "synthetic-camera", "/synthetic/previous.mp4", 0, 12],
            selected,
            ["next", "synthetic-camera", "/synthetic/next.mp4", 18, 30],
            ["other", "different-camera", "/synthetic/other.mp4", 0, 100],
        ]
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE TABLE recordings (id TEXT, camera TEXT, start_time REAL, end_time REAL)")
            connection.executemany("INSERT INTO recordings VALUES (?,?,?,?)",
                                   [(r[0], r[1], r[3], r[4]) for r in rows])
            start, end = recording_playback_interval(selected, rows)
            actual = connection.execute(
                "SELECT id FROM recordings WHERE camera=? AND "
                "(start_time BETWEEN ? AND ? OR end_time BETWEEN ? AND ? "
                "OR (? > start_time AND ? < end_time))",
                ("synthetic-camera", start, end, start, end, start, end)).fetchall()
        self.assertEqual(actual, [("selected",)])
        self.assertGreater(start, 12)
        self.assertLess(end, 18)

    def test_interior_overlap_keeps_an_exclusive_complete_recording_window(self):
        selected = ["selected", "synthetic-camera", "/synthetic/new.mp4", 10, 30]
        overlap = ["overlap", "synthetic-camera", "/synthetic/other.mp4", 14, 25]
        start, end = recording_playback_interval(selected, [selected, overlap])
        self.assertGreater(start, 25)
        self.assertLess(end, 30)

    def test_recovery_rejects_new_segment_fully_covered_by_older_recording(self):
        old = ["old", "synthetic-camera", "/synthetic/old.mp4", 0, 40]
        new = ["new", "synthetic-camera", "/synthetic/new.mp4", 20, 30]
        self.assertIsNone(recording_for_playback([old, new], "synthetic-camera", 10))

    def test_playback_requires_a_real_decoded_frame_not_only_a_success_exit(self):
        frame = '0, 0, 0, 1, 1024, ' + 'a' * 64
        self.assertEqual(decoded_frame_hash('#format: frame checksums\n' + frame), 'a' * 64)
        for output in ('', '#format: frame checksums\n', 'not a frame', frame.replace('1024', '0'), frame + '\n' + frame):
            with self.subTest(output=output), self.assertRaises(AssertionError):
                decoded_frame_hash(output)


class FrigateControlFaultFixtureTests(unittest.TestCase):
    def test_embedded_control_fault_is_valid_standalone_python(self):
        compile(FRIGATE_CONTROL_FAULT_SCRIPT, "synthetic-frigate-control-fault", "exec")


class FrigateReaderReadinessTests(unittest.TestCase):
    def setUp(self):
        self.ids = {"first": "synthetic-first", "second": "synthetic-second"}
        self.revisions = {uid: "address-current" for uid in self.ids.values()}
        self.rows = {key: {"camera_uuid": uid, "relay_address_revision": "address-current",
                          "relay_runtime_revision": "runtime-current"} for key, uid in self.ids.items()}

    def test_recording_checkpoint_waits_for_both_committed_readers(self):
        self.assertTrue(reader_recovery_ready(self.rows, self.ids, self.revisions))
        for key in self.ids:
            for field, value in (("capture_resume_pending", True), ("source_recovery_pending", True), ("target_restart", True),
                                 ("relay_address_revision", "address-previous"), ("camera_uuid", "unrelated")):
                with self.subTest(key=key, field=field):
                    changed = {name: dict(row) for name, row in self.rows.items()}
                    changed[key][field] = value
                    self.assertFalse(reader_recovery_ready(changed, self.ids, self.revisions))

    def test_restart_waits_for_a_new_consistent_runtime_for_each_reader(self):
        previous = {key: "runtime-current" for key in self.ids}
        self.assertFalse(reader_recovery_ready(self.rows, self.ids, self.revisions, previous))
        self.rows["first"]["relay_runtime_revision"] = "runtime-new"
        self.assertFalse(reader_recovery_ready(self.rows, self.ids, self.revisions, previous))
        self.rows["second"]["relay_runtime_revision"] = "runtime-new"
        self.assertTrue(reader_recovery_ready(self.rows, self.ids, self.revisions, previous))

    def test_missing_readers_or_generation_cannot_mark_new_runtime_ready(self):
        self.assertFalse(reader_recovery_ready({}, self.ids, self.revisions))
        self.assertFalse(reader_recovery_ready(self.rows, self.ids, {}))
        self.rows["first"].pop("relay_runtime_revision")
        self.assertFalse(reader_recovery_ready(self.rows, self.ids, self.revisions))


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
