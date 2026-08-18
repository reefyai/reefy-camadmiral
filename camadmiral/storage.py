from __future__ import annotations

import hashlib
import json
import sqlite3
import secrets
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .crypto import decrypt_password, encrypt_password
from .media import ProbeResult

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE camera_credentials (
        credential_uuid TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        password_ciphertext BLOB NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE cameras (
        camera_uuid TEXT PRIMARY KEY,
        candidate_uuid TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        credential_uuid TEXT NOT NULL REFERENCES camera_credentials(credential_uuid),
        adopted_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE onvif_profiles (
        profile_uuid TEXT PRIMARY KEY,
        camera_uuid TEXT NOT NULL REFERENCES cameras(camera_uuid) ON DELETE CASCADE,
        profile_token TEXT NOT NULL,
        name TEXT NOT NULL,
        width INTEGER NOT NULL,
        height INTEGER NOT NULL,
        encoding TEXT,
        fps REAL NOT NULL,
        bitrate_kbps INTEGER NOT NULL,
        uri TEXT,
        updated_at TEXT NOT NULL,
        UNIQUE(camera_uuid, profile_token)
    );
    CREATE TABLE managed_streams (
        stream_uuid TEXT PRIMARY KEY,
        camera_uuid TEXT NOT NULL REFERENCES cameras(camera_uuid) ON DELETE CASCADE,
        profile_uuid TEXT NOT NULL UNIQUE REFERENCES onvif_profiles(profile_uuid) ON DELETE CASCADE,
        stream_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    );
    CREATE TABLE consumer_bindings (
        camera_uuid TEXT NOT NULL REFERENCES cameras(camera_uuid) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK(role IN ('record', 'detect')),
        stream_uuid TEXT NOT NULL REFERENCES managed_streams(stream_uuid),
        updated_at TEXT NOT NULL,
        PRIMARY KEY(camera_uuid, role)
    );
    """,
    """
    ALTER TABLE managed_streams ADD COLUMN probe_status TEXT NOT NULL DEFAULT 'pending';
    ALTER TABLE managed_streams ADD COLUMN probed_at TEXT;
    ALTER TABLE managed_streams ADD COLUMN probe_latency_ms INTEGER;
    ALTER TABLE managed_streams ADD COLUMN video_codec TEXT;
    ALTER TABLE managed_streams ADD COLUMN audio_codec TEXT;
    ALTER TABLE managed_streams ADD COLUMN probed_width INTEGER;
    ALTER TABLE managed_streams ADD COLUMN probed_height INTEGER;
    ALTER TABLE managed_streams ADD COLUMN probed_fps REAL;
    """,
    """
    CREATE TABLE service_secrets (
        name TEXT PRIMARY KEY,
        secret_ciphertext BLOB NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    ALTER TABLE onvif_profiles ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'onvif';
    ALTER TABLE onvif_profiles ADD COLUMN source_scheme TEXT;
    ALTER TABLE onvif_profiles ADD COLUMN source_host TEXT;
    ALTER TABLE onvif_profiles ADD COLUMN source_port INTEGER;
    ALTER TABLE onvif_profiles ADD COLUMN source_path TEXT;
    ALTER TABLE onvif_profiles ADD COLUMN source_query TEXT;
    """,
    """
    ALTER TABLE onvif_profiles ADD COLUMN catalog_revision TEXT;
    ALTER TABLE onvif_profiles ADD COLUMN catalog_rule_id TEXT;
    ALTER TABLE onvif_profiles ADD COLUMN catalog_source_url TEXT;
    ALTER TABLE managed_streams ADD COLUMN health_status TEXT NOT NULL DEFAULT 'unknown';
    ALTER TABLE managed_streams ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE managed_streams ADD COLUMN last_ready_at TEXT;
    ALTER TABLE managed_streams ADD COLUMN last_failure_at TEXT;
    """,
    """
    CREATE TABLE camera_address_events (
        event_uuid TEXT PRIMARY KEY,
        camera_uuid TEXT NOT NULL REFERENCES cameras(camera_uuid) ON DELETE CASCADE,
        previous_address TEXT NOT NULL,
        current_address TEXT NOT NULL,
        evidence TEXT NOT NULL,
        changed_at TEXT NOT NULL
    );
    CREATE INDEX camera_address_events_camera_time
        ON camera_address_events(camera_uuid, changed_at);
    """,
    """
    CREATE TABLE media_config_revisions (
        revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
        desired_hash TEXT NOT NULL,
        config_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('desired', 'applied', 'failed')),
        error_code TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT
    );
    CREATE INDEX media_config_revisions_status_revision
        ON media_config_revisions(status, revision_id);
    """,
    """
    CREATE TABLE frigate_bindings (
        target_id TEXT NOT NULL,
        camera_uuid TEXT NOT NULL REFERENCES cameras(camera_uuid) ON DELETE CASCADE,
        frigate_camera_key TEXT NOT NULL,
        record_stream_uuid TEXT NOT NULL REFERENCES managed_streams(stream_uuid),
        detect_stream_uuid TEXT NOT NULL REFERENCES managed_streams(stream_uuid),
        desired_hash TEXT NOT NULL,
        applied_hash TEXT,
        status TEXT NOT NULL CHECK(status IN ('pending', 'applied', 'error')),
        last_error_code TEXT,
        last_attempt_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(target_id, camera_uuid),
        UNIQUE(target_id, frigate_camera_key)
    );
    CREATE INDEX frigate_bindings_target_status
        ON frigate_bindings(target_id, status);
    """,
    """
    ALTER TABLE cameras ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1
        CHECK(enabled IN (0, 1));
    ALTER TABLE frigate_bindings ADD COLUMN camera_enabled_applied INTEGER NOT NULL DEFAULT 1
        CHECK(camera_enabled_applied IN (0, 1));
    """,
    """
    CREATE TABLE camera_health_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        camera_uuid TEXT NOT NULL REFERENCES cameras(camera_uuid) ON DELETE CASCADE,
        state TEXT NOT NULL CHECK(state IN (
            'healthy', 'degraded', 'offline', 'auth_failed', 'unknown', 'disabled'
        )),
        reason TEXT NOT NULL,
        observed_at TEXT NOT NULL
    );
    CREATE INDEX camera_health_events_camera_time
        ON camera_health_events(camera_uuid, observed_at, event_id);
    """,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _source_parts(uri: str | None) -> tuple[str | None, str | None, int | None, str | None, str | None]:
    if not uri:
        return None, None, None, None, None
    parsed = urllib.parse.urlsplit(uri)
    return parsed.scheme, parsed.hostname, parsed.port, parsed.path, parsed.query


def _source_uri(row: sqlite3.Row) -> str:
    if row["source_scheme"] and row["source_host"]:
        host = f'[{row["source_host"]}]' if ":" in row["source_host"] else row["source_host"]
        if row["source_port"]:
            host = f'{host}:{row["source_port"]}'
        return urllib.parse.urlunsplit(
            (
                row["source_scheme"],
                host,
                row["source_path"] or "",
                row["source_query"] or "",
                "",
            )
        )
    return str(row["uri"])


class CameraRepository:
    def __init__(self, path: Path, master_key: bytes):
        self.path = path
        self.master_key = master_key

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        self.path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for version, migration in enumerate(MIGRATIONS, start=1):
                if version in current:
                    continue
                connection.executescript(migration)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, _now()),
                )
                if version == 10:
                    timestamp = _now()
                    for row in connection.execute("SELECT camera_uuid FROM cameras"):
                        self._record_camera_health_transition(
                            connection,
                            str(row["camera_uuid"]),
                            timestamp,
                        )
                connection.commit()

    @staticmethod
    def _camera_health_state(
        connection: sqlite3.Connection,
        camera_uuid: str,
    ) -> tuple[str, str] | None:
        camera = connection.execute(
            "SELECT enabled FROM cameras WHERE camera_uuid = ?",
            (camera_uuid,),
        ).fetchone()
        if camera is None:
            return None
        if not bool(camera["enabled"]):
            return "disabled", "operator_disabled"
        states = [
            str(row["health_status"])
            for row in connection.execute(
                "SELECT health_status FROM managed_streams WHERE camera_uuid = ?",
                (camera_uuid,),
            )
        ]
        if not states or all(state == "unknown" for state in states):
            return "unknown", "media_check_pending"
        if "auth_failed" in states:
            return "auth_failed", "authentication_failed"
        if all(state == "healthy" for state in states):
            return "healthy", "all_streams_healthy"
        if all(state == "offline" for state in states):
            return "offline", "all_streams_offline"
        if "offline" in states:
            return "degraded", "partial_stream_failure"
        if "degraded" in states:
            return "degraded", "media_probe_failed"
        return "unknown", "media_check_pending"

    @classmethod
    def _record_camera_health_transition(
        cls,
        connection: sqlite3.Connection,
        camera_uuid: str,
        timestamp: str,
    ) -> None:
        current = cls._camera_health_state(connection, camera_uuid)
        if current is None:
            return
        state, reason = current
        previous = connection.execute(
            "SELECT state, reason FROM camera_health_events WHERE camera_uuid = ? "
            "ORDER BY observed_at DESC, event_id DESC LIMIT 1",
            (camera_uuid,),
        ).fetchone()
        if previous is not None and previous["state"] == state and previous["reason"] == reason:
            return
        connection.execute(
            "INSERT INTO camera_health_events(camera_uuid, state, reason, observed_at) "
            "VALUES (?, ?, ?, ?)",
            (camera_uuid, state, reason, timestamp),
        )

    def camera_availability(
        self,
        camera_uuid: str,
        *,
        hours: int,
        bucket_count: int,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        if hours <= 0 or bucket_count <= 0:
            raise ValueError("Availability window must be positive")
        window_end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        window_start = window_end - timedelta(hours=hours)
        with self.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM cameras WHERE camera_uuid = ?",
                (camera_uuid,),
            ).fetchone() is None:
                return None
            previous = connection.execute(
                "SELECT state, reason, observed_at FROM camera_health_events "
                "WHERE camera_uuid = ? AND observed_at < ? "
                "ORDER BY observed_at DESC, event_id DESC LIMIT 1",
                (camera_uuid, window_start.isoformat()),
            ).fetchone()
            rows = connection.execute(
                "SELECT state, reason, observed_at FROM camera_health_events "
                "WHERE camera_uuid = ? AND observed_at >= ? AND observed_at <= ? "
                "ORDER BY observed_at, event_id",
                (camera_uuid, window_start.isoformat(), window_end.isoformat()),
            ).fetchall()

        state = str(previous["state"]) if previous is not None else "unknown"
        reason = str(previous["reason"]) if previous is not None else "no_observation"
        cursor = window_start
        segments: list[tuple[datetime, datetime, str, str]] = []
        for row in rows:
            observed_at = min(window_end, max(window_start, _parse_timestamp(str(row["observed_at"]))))
            if observed_at > cursor:
                segments.append((cursor, observed_at, state, reason))
            state = str(row["state"])
            reason = str(row["reason"])
            cursor = observed_at
        if cursor < window_end:
            segments.append((cursor, window_end, state, reason))

        observed_seconds = 0.0
        healthy_seconds = 0.0
        for start, end, segment_state, _ in segments:
            duration = (end - start).total_seconds()
            if segment_state in {"healthy", "degraded", "offline", "auth_failed"}:
                observed_seconds += duration
                if segment_state == "healthy":
                    healthy_seconds += duration

        bucket_seconds = (window_end - window_start).total_seconds() / bucket_count
        buckets: list[dict[str, str]] = []
        for position in range(bucket_count):
            bucket_start = window_start + timedelta(seconds=bucket_seconds * position)
            bucket_end = window_start + timedelta(seconds=bucket_seconds * (position + 1))
            overlapping = [
                segment
                for segment in segments
                if segment[0] < bucket_end and segment[1] > bucket_start
            ]
            states = [segment[2] for segment in overlapping]
            if "auth_failed" in states:
                bucket_state = "auth_failed"
            elif "offline" in states:
                bucket_state = "offline"
            elif "degraded" in states:
                bucket_state = "degraded"
            elif any(value in {"unknown", "disabled"} for value in states):
                bucket_state = "disabled" if states and all(value == "disabled" for value in states) else "unknown"
            else:
                bucket_state = "healthy" if states else "unknown"
            bucket_reason = next(
                (
                    segment[3]
                    for segment in reversed(overlapping)
                    if segment[2] == bucket_state
                ),
                overlapping[-1][3] if overlapping else "no_observation",
            )
            buckets.append(
                {
                    "start": bucket_start.isoformat(),
                    "end": bucket_end.isoformat(),
                    "state": bucket_state,
                    "reason": bucket_reason,
                }
            )

        return {
            "window": f"{hours}h",
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "availability_percent": (
                round(healthy_seconds * 100 / observed_seconds, 2)
                if observed_seconds > 0
                else None
            ),
            "observed_seconds": round(observed_seconds, 3),
            "buckets": buckets,
        }

    def adoption_for_candidate(self, candidate_uuid: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            camera = connection.execute(
                "SELECT camera_uuid, candidate_uuid, display_name, credential_uuid, adopted_at, enabled "
                "FROM cameras WHERE candidate_uuid = ?",
                (candidate_uuid,),
            ).fetchone()
            if camera is None:
                return None
            roles = {
                row["role"]: row["stream_uuid"]
                for row in connection.execute(
                    "SELECT role, stream_uuid FROM consumer_bindings WHERE camera_uuid = ?",
                    (camera["camera_uuid"],),
                )
            }
            role_tokens = {
                row["role"]: row["profile_token"]
                for row in connection.execute(
                    "SELECT b.role, p.profile_token FROM consumer_bindings b "
                    "JOIN managed_streams s USING (stream_uuid) "
                    "JOIN onvif_profiles p USING (profile_uuid) WHERE b.camera_uuid = ?",
                    (camera["camera_uuid"],),
                )
            }
            streams = [
                dict(row)
                for row in connection.execute(
                    "SELECT p.profile_token, p.source_kind, p.name, p.width, p.height, p.encoding, p.fps, p.uri, "
                    "p.catalog_revision, p.catalog_rule_id, p.catalog_source_url, "
                    "s.stream_uuid, s.stream_key, s.probe_status, s.probed_at, "
                    "s.probe_latency_ms, s.video_codec, s.audio_codec, s.probed_width, s.probed_height, s.probed_fps, "
                    "s.health_status, s.consecutive_failures, s.last_ready_at, s.last_failure_at "
                    "FROM managed_streams s JOIN onvif_profiles p USING (profile_uuid) "
                    "WHERE s.camera_uuid = ? ORDER BY p.profile_token",
                    (camera["camera_uuid"],),
                )
            ]
            return {
                "camera_uuid": camera["camera_uuid"],
                "candidate_uuid": camera["candidate_uuid"],
                "display_name": camera["display_name"],
                "enabled": bool(camera["enabled"]),
                "adopted_at": camera["adopted_at"],
                "roles": roles,
                "role_tokens": role_tokens,
                "streams": streams,
            }

    def adoption_map(self) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            candidates = [row["candidate_uuid"] for row in connection.execute("SELECT candidate_uuid FROM cameras")]
        return {
            candidate_uuid: adoption
            for candidate_uuid in candidates
            if (adoption := self.adoption_for_candidate(candidate_uuid)) is not None
        }

    def consumer_inventory(self) -> list[dict[str, Any]]:
        """Return adopted camera metadata without upstream URLs or credentials."""
        with self.connect() as connection:
            cameras = connection.execute(
                "SELECT camera_uuid, candidate_uuid, display_name, enabled FROM cameras "
                "ORDER BY display_name COLLATE NOCASE, camera_uuid"
            ).fetchall()
            inventory: list[dict[str, Any]] = []
            for camera in cameras:
                roles: dict[str, list[str]] = {}
                for row in connection.execute(
                    "SELECT role, stream_uuid FROM consumer_bindings WHERE camera_uuid = ? "
                    "ORDER BY role",
                    (camera["camera_uuid"],),
                ):
                    roles.setdefault(str(row["stream_uuid"]), []).append(str(row["role"]))
                streams = []
                for row in connection.execute(
                    "SELECT s.stream_uuid, s.stream_key, s.health_status, "
                    "s.video_codec, s.probed_width, s.probed_height, s.probed_fps, "
                    "p.encoding, p.width, p.height, p.fps "
                    "FROM managed_streams s JOIN onvif_profiles p USING (profile_uuid) "
                    "WHERE s.camera_uuid = ? ORDER BY s.stream_uuid",
                    (camera["camera_uuid"],),
                ):
                    stream = dict(row)
                    stream["roles"] = roles.get(str(row["stream_uuid"]), [])
                    streams.append(stream)
                inventory.append(
                    {
                        "camera_uuid": str(camera["camera_uuid"]),
                        "candidate_uuid": str(camera["candidate_uuid"]),
                        "display_name": str(camera["display_name"]),
                        "enabled": bool(camera["enabled"]),
                        "streams": streams,
                    }
                )
        return inventory

    def preview_stream_for_camera(self, camera_uuid: str) -> dict[str, str] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT s.stream_uuid, s.stream_key, s.health_status "
                "FROM managed_streams s "
                "JOIN cameras c ON c.camera_uuid = s.camera_uuid "
                "LEFT JOIN consumer_bindings b ON b.camera_uuid = s.camera_uuid "
                "AND b.stream_uuid = s.stream_uuid "
                "WHERE s.camera_uuid = ? AND c.enabled = 1 "
                "ORDER BY CASE s.health_status WHEN 'healthy' THEN 0 WHEN 'degraded' THEN 1 ELSE 2 END, "
                "CASE b.role WHEN 'detect' THEN 0 WHEN 'record' THEN 1 ELSE 2 END, s.stream_key "
                "LIMIT 1",
                (camera_uuid,),
            ).fetchone()
        return dict(row) if row is not None else None

    def camera(self, camera_uuid: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT camera_uuid, candidate_uuid, display_name, enabled FROM cameras WHERE camera_uuid = ?",
                (camera_uuid,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    def update_camera_name(self, camera_uuid: str, display_name: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE cameras SET display_name = ?, updated_at = ? WHERE camera_uuid = ?",
                (display_name, _now(), camera_uuid),
            )
            connection.commit()
        return cursor.rowcount == 1

    def set_camera_enabled(self, camera_uuid: str, enabled: bool) -> bool:
        timestamp = _now()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE cameras SET enabled = ?, updated_at = ? WHERE camera_uuid = ?",
                (int(enabled), timestamp, camera_uuid),
            )
            if cursor.rowcount == 1:
                self._record_camera_health_transition(connection, camera_uuid, timestamp)
            connection.commit()
        return cursor.rowcount == 1

    def replace_camera_credentials(
        self,
        camera_uuid: str,
        username: str,
        password: str,
    ) -> bool:
        timestamp = _now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            camera = connection.execute(
                "SELECT credential_uuid FROM cameras WHERE camera_uuid = ?",
                (camera_uuid,),
            ).fetchone()
            if camera is None:
                connection.rollback()
                return False
            credential_uuid = str(camera["credential_uuid"])
            connection.execute(
                "UPDATE camera_credentials SET username = ?, password_ciphertext = ?, updated_at = ? "
                "WHERE credential_uuid = ?",
                (
                    username,
                    encrypt_password(password, credential_uuid, self.master_key),
                    timestamp,
                    credential_uuid,
                ),
            )
            connection.execute(
                "UPDATE cameras SET updated_at = ? WHERE camera_uuid = ?",
                (timestamp, camera_uuid),
            )
            connection.execute(
                "UPDATE managed_streams SET probe_status = 'pending', probed_at = NULL, "
                "health_status = 'unknown', consecutive_failures = 0, last_failure_at = NULL "
                "WHERE camera_uuid = ?",
                (camera_uuid,),
            )
            self._record_camera_health_transition(connection, camera_uuid, timestamp)
            connection.commit()
        return True

    def credentials_for_candidate(self, candidate_uuid: str) -> tuple[str, str] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT c.credential_uuid, c.username, c.password_ciphertext "
                "FROM camera_credentials c JOIN cameras a USING (credential_uuid) "
                "WHERE a.candidate_uuid = ?",
                (candidate_uuid,),
            ).fetchone()
        if row is None:
            return None
        password = decrypt_password(row["password_ciphertext"], row["credential_uuid"], self.master_key)
        return str(row["username"]), password

    def rtsp_access_password(self) -> str:
        name = "rtsp-access"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT secret_ciphertext FROM service_secrets WHERE name = ?",
                (name,),
            ).fetchone()
            if row is not None:
                connection.commit()
                return decrypt_password(row["secret_ciphertext"], name, self.master_key)
            password = secrets.token_urlsafe(24)
            timestamp = _now()
            connection.execute(
                "INSERT INTO service_secrets(name, secret_ciphertext, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (name, encrypt_password(password, name, self.master_key), timestamp, timestamp),
            )
            connection.commit()
            return password

    def frigate_binding(self, target_id: str, camera_uuid: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM frigate_bindings WHERE target_id = ? AND camera_uuid = ?",
                (target_id, camera_uuid),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_frigate_attempt(
        self,
        target_id: str,
        camera_uuid: str,
        frigate_camera_key: str,
        record_stream_uuid: str,
        detect_stream_uuid: str,
        desired_hash: str,
    ) -> None:
        timestamp = _now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO frigate_bindings(target_id, camera_uuid, frigate_camera_key, "
                "record_stream_uuid, detect_stream_uuid, desired_hash, status, last_attempt_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?) "
                "ON CONFLICT(target_id, camera_uuid) DO UPDATE SET "
                "frigate_camera_key=excluded.frigate_camera_key, "
                "record_stream_uuid=excluded.record_stream_uuid, "
                "detect_stream_uuid=excluded.detect_stream_uuid, desired_hash=excluded.desired_hash, "
                "status='pending', last_error_code=NULL, last_attempt_at=excluded.last_attempt_at, "
                "updated_at=excluded.updated_at",
                (
                    target_id,
                    camera_uuid,
                    frigate_camera_key,
                    record_stream_uuid,
                    detect_stream_uuid,
                    desired_hash,
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()

    def complete_frigate_attempt(
        self,
        target_id: str,
        camera_uuid: str,
        *,
        status: str,
        applied_hash: str | None = None,
        error_code: str | None = None,
    ) -> None:
        if status not in {"applied", "error"}:
            raise ValueError("Frigate integration status is invalid")
        with self.connect() as connection:
            connection.execute(
                "UPDATE frigate_bindings SET status=?, "
                "applied_hash=CASE WHEN ?='applied' THEN ? ELSE applied_hash END, "
                "last_error_code=?, updated_at=? WHERE target_id=? AND camera_uuid=?",
                (
                    status,
                    status,
                    applied_hash,
                    error_code,
                    _now(),
                    target_id,
                    camera_uuid,
                ),
            )
            connection.commit()

    def frigate_bindings(self, target_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT b.target_id, b.camera_uuid, b.frigate_camera_key, b.status, "
                "b.last_error_code, b.last_attempt_at, b.updated_at, "
                "b.camera_enabled_applied, c.display_name "
                "FROM frigate_bindings b JOIN cameras c USING(camera_uuid) "
                "WHERE b.target_id = ? ORDER BY c.display_name COLLATE NOCASE, b.camera_uuid",
                (target_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_frigate_camera_enabled_applied(
        self,
        target_id: str,
        camera_uuid: str,
        enabled: bool,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE frigate_bindings SET camera_enabled_applied = ?, updated_at = ? "
                "WHERE target_id = ? AND camera_uuid = ?",
                (int(enabled), _now(), target_id, camera_uuid),
            )
            connection.commit()

    def adopt(
        self,
        candidate: dict[str, Any],
        username: str,
        password: str,
        profiles: list[dict[str, Any]],
        roles: dict[str, str],
    ) -> dict[str, Any]:
        candidate_uuid = str(candidate["candidate_uuid"])
        timestamp = _now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT camera_uuid, credential_uuid FROM cameras WHERE candidate_uuid = ?",
                (candidate_uuid,),
            ).fetchone()
            camera_uuid = str(existing["camera_uuid"]) if existing else str(uuid.uuid4())
            credential_uuid = str(existing["credential_uuid"]) if existing else str(uuid.uuid4())
            encrypted = encrypt_password(password, credential_uuid, self.master_key)
            connection.execute(
                "INSERT INTO camera_credentials(credential_uuid, username, password_ciphertext, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(credential_uuid) DO UPDATE SET "
                "username=excluded.username, password_ciphertext=excluded.password_ciphertext, updated_at=excluded.updated_at",
                (credential_uuid, username, encrypted, timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO cameras(camera_uuid, candidate_uuid, display_name, credential_uuid, adopted_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(candidate_uuid) DO UPDATE SET "
                "display_name=excluded.display_name, credential_uuid=excluded.credential_uuid, updated_at=excluded.updated_at",
                (
                    camera_uuid,
                    candidate_uuid,
                    str(candidate.get("display_name") or candidate.get("ip") or "Camera"),
                    credential_uuid,
                    timestamp,
                    timestamp,
                ),
            )
            current_tokens = {str(profile["token"]) for profile in profiles}
            connection.execute("DELETE FROM consumer_bindings WHERE camera_uuid = ?", (camera_uuid,))
            if current_tokens:
                placeholders = ",".join("?" for _ in current_tokens)
                connection.execute(
                    f"DELETE FROM onvif_profiles WHERE camera_uuid = ? AND profile_token NOT IN ({placeholders})",
                    (camera_uuid, *sorted(current_tokens)),
                )
            streams_by_token: dict[str, str] = {}
            for profile in profiles:
                token = str(profile["token"])
                source_scheme, source_host, source_port, source_path, source_query = _source_parts(
                    profile.get("uri")
                )
                existing_profile = connection.execute(
                    "SELECT profile_uuid FROM onvif_profiles WHERE camera_uuid = ? AND profile_token = ?",
                    (camera_uuid, token),
                ).fetchone()
                profile_uuid = str(existing_profile["profile_uuid"]) if existing_profile else str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO onvif_profiles(profile_uuid, camera_uuid, profile_token, name, width, height, "
                    "encoding, fps, bitrate_kbps, uri, updated_at, source_kind, source_scheme, source_host, "
                    "source_port, source_path, source_query, catalog_revision, catalog_rule_id, catalog_source_url) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(camera_uuid, profile_token) DO UPDATE SET name=excluded.name, width=excluded.width, "
                    "height=excluded.height, encoding=excluded.encoding, fps=excluded.fps, "
                    "bitrate_kbps=excluded.bitrate_kbps, uri=excluded.uri, updated_at=excluded.updated_at, "
                    "source_kind=excluded.source_kind, source_scheme=excluded.source_scheme, "
                    "source_host=excluded.source_host, source_port=excluded.source_port, "
                    "source_path=excluded.source_path, source_query=excluded.source_query, "
                    "catalog_revision=excluded.catalog_revision, catalog_rule_id=excluded.catalog_rule_id, "
                    "catalog_source_url=excluded.catalog_source_url",
                    (
                        profile_uuid,
                        camera_uuid,
                        token,
                        str(profile.get("name") or token),
                        int(profile.get("width") or 0),
                        int(profile.get("height") or 0),
                        profile.get("encoding"),
                        float(profile.get("fps") or 0),
                        int(profile.get("bitrate_kbps") or 0),
                        profile.get("uri"),
                        timestamp,
                        str(profile.get("source_kind") or "onvif"),
                        source_scheme,
                        source_host,
                        source_port,
                        source_path,
                        source_query,
                        profile.get("catalog_revision"),
                        profile.get("catalog_rule_id"),
                        profile.get("catalog_source_url"),
                    ),
                )
                stream = connection.execute(
                    "SELECT stream_uuid FROM managed_streams WHERE profile_uuid = ?",
                    (profile_uuid,),
                ).fetchone()
                stream_uuid = str(stream["stream_uuid"]) if stream else str(uuid.uuid4())
                if stream is None:
                    connection.execute(
                        "INSERT INTO managed_streams(stream_uuid, camera_uuid, profile_uuid, stream_key, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (stream_uuid, camera_uuid, profile_uuid, f"stream_{stream_uuid}", timestamp),
                    )
                else:
                    connection.execute(
                        "UPDATE managed_streams SET probe_status='pending', probed_at=NULL, "
                        "health_status='unknown', consecutive_failures=0, last_failure_at=NULL "
                        "WHERE stream_uuid = ?",
                        (stream_uuid,),
                    )
                streams_by_token[token] = stream_uuid
            for role, token in roles.items():
                stream_uuid = streams_by_token.get(token)
                if stream_uuid is None:
                    continue
                connection.execute(
                    "INSERT INTO consumer_bindings(camera_uuid, role, stream_uuid, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(camera_uuid, role) DO UPDATE SET stream_uuid=excluded.stream_uuid, updated_at=excluded.updated_at",
                    (camera_uuid, role, stream_uuid, timestamp),
                )
            self._record_camera_health_transition(connection, camera_uuid, timestamp)
            connection.commit()
        adoption = self.adoption_for_candidate(candidate_uuid)
        assert adoption is not None
        return adoption

    def managed_stream_sources(
        self,
        *,
        include_auth_failed: bool = True,
        include_disabled: bool = False,
        camera_uuid: str | None = None,
        role_bound_only: bool = False,
    ) -> list[dict[str, str]]:
        health_filter = "" if include_auth_failed else "AND s.health_status != 'auth_failed' "
        enabled_filter = "" if include_disabled else "AND a.enabled = 1 "
        camera_filter = "AND s.camera_uuid = ? " if camera_uuid is not None else ""
        role_join = "JOIN consumer_bindings b ON b.stream_uuid = s.stream_uuid " if role_bound_only else ""
        parameters = (camera_uuid,) if camera_uuid is not None else ()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT s.stream_uuid, s.stream_key, s.camera_uuid, p.uri, p.source_scheme, p.source_host, p.source_port, "
                "p.source_path, p.source_query, c.username, c.password_ciphertext, c.credential_uuid "
                "FROM managed_streams s JOIN onvif_profiles p USING (profile_uuid) "
                "JOIN cameras a USING (camera_uuid) JOIN camera_credentials c USING (credential_uuid) "
                f"{role_join}WHERE p.uri IS NOT NULL {health_filter}{enabled_filter}{camera_filter}"
                "ORDER BY s.camera_uuid, s.stream_key",
                parameters,
            ).fetchall()
        sources = []
        for row in rows:
            sources.append(
                {
                    "stream_uuid": str(row["stream_uuid"]),
                    "stream_key": str(row["stream_key"]),
                    "camera_uuid": str(row["camera_uuid"]),
                    "uri": _source_uri(row),
                    "username": str(row["username"]),
                    "password": decrypt_password(row["password_ciphertext"], row["credential_uuid"], self.master_key),
                    "credential_uuid": str(row["credential_uuid"]),
                }
            )
        return sources

    def record_desired_media_revision(
        self,
        sources: list[dict[str, str]],
    ) -> tuple[int, str]:
        typed_sources = []
        for source in sorted(sources, key=lambda item: item["stream_key"]):
            parsed = urllib.parse.urlsplit(source["uri"])
            typed_sources.append(
                {
                    "stream_uuid": source["stream_uuid"],
                    "stream_key": source["stream_key"],
                    "credential_ref": source["credential_uuid"],
                    "source": {
                        "scheme": parsed.scheme,
                        "host": parsed.hostname,
                        "port": parsed.port,
                        "path": parsed.path,
                        "query": parsed.query,
                    },
                }
            )
        config_json = json.dumps(
            {"streams": typed_sources},
            sort_keys=True,
            separators=(",", ":"),
        )
        desired_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
        with self.connect() as connection:
            latest = connection.execute(
                "SELECT revision_id, desired_hash, status FROM media_config_revisions "
                "ORDER BY revision_id DESC LIMIT 1"
            ).fetchone()
            if latest is not None and latest["desired_hash"] == desired_hash and latest["status"] != "failed":
                return int(latest["revision_id"]), str(latest["status"])
            cursor = connection.execute(
                "INSERT INTO media_config_revisions(desired_hash, config_json, status, created_at) "
                "VALUES (?, ?, 'desired', ?)",
                (desired_hash, config_json, _now()),
            )
            connection.commit()
            return int(cursor.lastrowid), "desired"

    def complete_media_revision(
        self,
        revision_id: int,
        status: str,
        error_code: str | None = None,
    ) -> None:
        if status not in {"applied", "failed"}:
            raise ValueError("Media revision completion status is invalid")
        with self.connect() as connection:
            connection.execute(
                "UPDATE media_config_revisions SET status=?, error_code=?, completed_at=? "
                "WHERE revision_id=? AND status='desired'",
                (status, error_code, _now(), revision_id),
            )
            connection.commit()

    def last_known_good_media_revision(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT revision_id, desired_hash, config_json, created_at, completed_at "
                "FROM media_config_revisions WHERE status='applied' "
                "ORDER BY revision_id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["config"] = json.loads(result.pop("config_json"))
        return result

    def update_profile_sources(
        self,
        camera_uuid: str,
        sources_by_token: dict[str, str],
    ) -> None:
        if not sources_by_token:
            return
        timestamp = _now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            known_tokens = {
                str(row["profile_token"])
                for row in connection.execute(
                    "SELECT profile_token FROM onvif_profiles WHERE camera_uuid = ?",
                    (camera_uuid,),
                )
            }
            if not set(sources_by_token).issubset(known_tokens):
                connection.rollback()
                raise ValueError("Cannot update an unknown camera profile")
            for token, uri in sources_by_token.items():
                source_scheme, source_host, source_port, source_path, source_query = _source_parts(uri)
                connection.execute(
                    "UPDATE onvif_profiles SET uri=?, source_scheme=?, source_host=?, source_port=?, "
                    "source_path=?, source_query=?, updated_at=? WHERE camera_uuid=? AND profile_token=?",
                    (
                        uri,
                        source_scheme,
                        source_host,
                        source_port,
                        source_path,
                        source_query,
                        timestamp,
                        camera_uuid,
                        token,
                    ),
                )
            connection.execute(
                "UPDATE managed_streams SET probe_status='pending', probed_at=NULL, health_status='unknown', "
                "consecutive_failures=0, last_failure_at=NULL WHERE camera_uuid=? AND profile_uuid IN "
                "(SELECT profile_uuid FROM onvif_profiles WHERE camera_uuid=? AND profile_token IN "
                f"({','.join('?' for _ in sources_by_token)}))",
                (camera_uuid, camera_uuid, *sources_by_token.keys()),
            )
            self._record_camera_health_transition(connection, camera_uuid, timestamp)
            connection.commit()

    def record_address_change(
        self,
        camera_uuid: str,
        previous_address: str,
        current_address: str,
        evidence: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO camera_address_events(event_uuid, camera_uuid, previous_address, "
                "current_address, evidence, changed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    camera_uuid,
                    previous_address,
                    current_address,
                    evidence,
                    _now(),
                ),
            )
            connection.commit()

    def record_probe_results(self, results: dict[str, ProbeResult]) -> None:
        timestamp = _now()
        with self.connect() as connection:
            affected_cameras: set[str] = set()
            for stream_uuid, result in results.items():
                current = connection.execute(
                    "SELECT camera_uuid, consecutive_failures FROM managed_streams WHERE stream_uuid = ?",
                    (stream_uuid,),
                ).fetchone()
                if current is None:
                    continue
                affected_cameras.add(str(current["camera_uuid"]))
                failures = 0 if result.status == "ready" else int(current["consecutive_failures"]) + 1
                if result.status == "ready":
                    health_status = "healthy"
                elif result.status == "auth_failed":
                    health_status = "auth_failed"
                elif failures >= 3:
                    health_status = "offline"
                else:
                    health_status = "degraded"
                connection.execute(
                    "UPDATE managed_streams SET probe_status=?, probed_at=?, probe_latency_ms=?, "
                    "health_status=?, consecutive_failures=?, "
                    "last_ready_at=CASE WHEN ?='ready' THEN ? ELSE last_ready_at END, "
                    "last_failure_at=CASE WHEN ?='ready' THEN last_failure_at ELSE ? END "
                    "WHERE stream_uuid=?",
                    (
                        result.status,
                        timestamp,
                        result.latency_ms,
                        health_status,
                        failures,
                        result.status,
                        timestamp,
                        result.status,
                        timestamp,
                        stream_uuid,
                    ),
                )
                if result.status == "ready":
                    connection.execute(
                        "UPDATE managed_streams SET video_codec=?, audio_codec=?, probed_width=?, "
                        "probed_height=?, probed_fps=? WHERE stream_uuid=?",
                        (
                            result.video_codec,
                            result.audio_codec,
                            result.width,
                            result.height,
                            result.fps,
                            stream_uuid,
                        ),
                    )
            for camera_uuid in affected_cameras:
                self._record_camera_health_transition(connection, camera_uuid, timestamp)
            connection.commit()

    def record_camera_auth_failure(self, camera_uuid: str, result: ProbeResult) -> None:
        with self.connect() as connection:
            stream_ids = [
                str(row["stream_uuid"])
                for row in connection.execute(
                    "SELECT stream_uuid FROM managed_streams WHERE camera_uuid = ?",
                    (camera_uuid,),
                )
            ]
        self.record_probe_results({stream_uuid: result for stream_uuid in stream_ids})
