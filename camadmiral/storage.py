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
    """
    CREATE TABLE camera_incidents (
        incident_uuid TEXT PRIMARY KEY,
        camera_uuid TEXT NOT NULL REFERENCES cameras(camera_uuid) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('media_offline', 'authentication_failed')),
        severity TEXT NOT NULL DEFAULT 'critical' CHECK(severity IN ('critical')),
        opened_at TEXT NOT NULL,
        last_observed_at TEXT NOT NULL,
        resolved_at TEXT,
        resolution_reason TEXT
    );
    CREATE UNIQUE INDEX camera_incidents_one_open_per_camera
        ON camera_incidents(camera_uuid) WHERE resolved_at IS NULL;
    CREATE INDEX camera_incidents_status_time
        ON camera_incidents(resolved_at, opened_at DESC);

    CREATE TABLE notification_settings (
        singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
        provider TEXT NOT NULL DEFAULT 'telegram' CHECK(provider = 'telegram'),
        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
        bot_token_ciphertext BLOB,
        bot_id TEXT,
        bot_username TEXT,
        chat_id TEXT,
        chat_label TEXT,
        pairing_token_ciphertext BLOB,
        pairing_expires_at TEXT,
        update_offset INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE notification_outbox (
        outbox_uuid TEXT PRIMARY KEY,
        incident_uuid TEXT REFERENCES camera_incidents(incident_uuid) ON DELETE SET NULL,
        event_type TEXT NOT NULL CHECK(event_type IN ('incident_opened', 'incident_resolved', 'test')),
        payload_json TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status IN ('pending', 'retry', 'sent', 'failed')),
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        provider_message_id TEXT,
        last_error_code TEXT,
        created_at TEXT NOT NULL,
        sent_at TEXT
    );
    CREATE INDEX notification_outbox_due
        ON notification_outbox(status, next_attempt_at);
    """,
    """
    UPDATE notification_settings
    SET enabled = 1
    WHERE bot_token_ciphertext IS NOT NULL;
    """,
    """
    CREATE TABLE frigate_targets (
        target_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        api_url TEXT NOT NULL UNIQUE,
        sync_cameras INTEGER NOT NULL DEFAULT 1 CHECK(sync_cameras IN (0, 1)),
        connection_status TEXT NOT NULL DEFAULT 'pending'
            CHECK(connection_status IN ('pending', 'connected', 'error')),
        last_error_code TEXT,
        last_checked_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE frigate_camera_selections (
        target_id TEXT NOT NULL REFERENCES frigate_targets(target_id) ON DELETE CASCADE,
        camera_uuid TEXT NOT NULL REFERENCES cameras(camera_uuid) ON DELETE CASCADE,
        selected_at TEXT NOT NULL,
        PRIMARY KEY(target_id, camera_uuid)
    );
    CREATE INDEX frigate_camera_selections_camera
        ON frigate_camera_selections(camera_uuid, target_id);
    """,
    """
    ALTER TABLE frigate_targets
    ADD COLUMN restart_recommended INTEGER NOT NULL DEFAULT 0
        CHECK(restart_recommended IN (0, 1));
    """,
    """
    ALTER TABLE frigate_camera_selections
    ADD COLUMN address_mode TEXT NOT NULL DEFAULT 'lan'
        CHECK(address_mode IN ('lan', 'localhost'));
    """,
    """
    CREATE TABLE discovery_settings (
        singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
        custom_subnets_json TEXT NOT NULL DEFAULT '[]',
        excluded_detected_subnets_json TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL
    );
    """,
    """
    ALTER TABLE discovery_settings
    ADD COLUMN excluded_custom_subnets_json TEXT NOT NULL DEFAULT '[]';
    """,
    """
    ALTER TABLE cameras
    ADD COLUMN stream_address_mode TEXT NOT NULL DEFAULT 'lan'
        CHECK(stream_address_mode IN ('lan', 'localhost'));
    UPDATE cameras
    SET stream_address_mode = (
        SELECT address_mode
        FROM frigate_camera_selections
        WHERE frigate_camera_selections.camera_uuid = cameras.camera_uuid
        ORDER BY selected_at DESC, target_id
        LIMIT 1
    )
    WHERE EXISTS (
        SELECT 1
        FROM frigate_camera_selections
        WHERE frigate_camera_selections.camera_uuid = cameras.camera_uuid
    );
    """,
    """
    CREATE TABLE blocked_devices (
        block_uuid TEXT PRIMARY KEY,
        candidate_uuid TEXT,
        onvif_identity TEXT,
        mac TEXT,
        display_name TEXT NOT NULL,
        last_ip TEXT,
        blocked_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(onvif_identity IS NOT NULL OR mac IS NOT NULL),
        UNIQUE(onvif_identity),
        UNIQUE(mac)
    );
    CREATE INDEX blocked_devices_candidate
        ON blocked_devices(candidate_uuid);
    """,
    """
    ALTER TABLE frigate_targets
    ADD COLUMN address_mode TEXT DEFAULT 'lan'
        CHECK(address_mode IN ('lan', 'localhost') OR address_mode IS NULL);
    UPDATE frigate_targets
    SET address_mode = (
        SELECT CASE
            WHEN COUNT(DISTINCT selections.address_mode) = 1
            THEN MIN(selections.address_mode)
            ELSE NULL
        END
        FROM frigate_camera_selections AS selections
        WHERE selections.target_id = frigate_targets.target_id
    )
    WHERE EXISTS (
        SELECT 1 FROM frigate_camera_selections AS selections
        WHERE selections.target_id = frigate_targets.target_id
    );
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
                if version == 11:
                    timestamp = _now()
                    for row in connection.execute("SELECT camera_uuid FROM cameras"):
                        self._reconcile_camera_incident(
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
                "SELECT DISTINCT s.health_status FROM managed_streams s "
                "JOIN consumer_bindings b ON b.stream_uuid = s.stream_uuid "
                "WHERE s.camera_uuid = ?",
                (camera_uuid,),
            )
        ]
        observed_states = [state for state in states if state != "unknown"]
        if not observed_states:
            return "unknown", "media_check_pending"
        if "auth_failed" in observed_states:
            return "auth_failed", "authentication_failed"
        if all(state == "healthy" for state in observed_states):
            return "healthy", "all_streams_healthy"
        if all(state == "offline" for state in observed_states):
            return "offline", "all_streams_offline"
        if "offline" in observed_states:
            return "degraded", "partial_stream_failure"
        if "degraded" in observed_states:
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
        if previous is None or previous["state"] != state or previous["reason"] != reason:
            connection.execute(
                "INSERT INTO camera_health_events(camera_uuid, state, reason, observed_at) "
                "VALUES (?, ?, ?, ?)",
                (camera_uuid, state, reason, timestamp),
            )
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='camera_incidents'"
        ).fetchone() is not None:
            cls._reconcile_camera_incident(connection, camera_uuid, timestamp)

    @classmethod
    def _reconcile_camera_incident(
        cls,
        connection: sqlite3.Connection,
        camera_uuid: str,
        timestamp: str,
    ) -> None:
        current = cls._camera_health_state(connection, camera_uuid)
        if current is None:
            return
        state, _ = current
        desired_kind = {
            "offline": "media_offline",
            "auth_failed": "authentication_failed",
        }.get(state)
        opened = connection.execute(
            "SELECT incident_uuid, kind FROM camera_incidents "
            "WHERE camera_uuid = ? AND resolved_at IS NULL",
            (camera_uuid,),
        ).fetchone()
        if desired_kind is not None:
            if opened is not None and opened["kind"] == desired_kind:
                connection.execute(
                    "UPDATE camera_incidents SET last_observed_at = ? WHERE incident_uuid = ?",
                    (timestamp, opened["incident_uuid"]),
                )
                return
            if opened is not None:
                cls._resolve_incident(
                    connection,
                    str(opened["incident_uuid"]),
                    timestamp,
                    "cause_changed",
                )
            incident_uuid = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO camera_incidents(incident_uuid, camera_uuid, kind, opened_at, "
                "last_observed_at) VALUES (?, ?, ?, ?, ?)",
                (incident_uuid, camera_uuid, desired_kind, timestamp, timestamp),
            )
            cls._enqueue_incident_notification(
                connection,
                incident_uuid,
                camera_uuid,
                desired_kind,
                "incident_opened",
                timestamp,
            )
            return
        if opened is not None and state in {"healthy", "disabled"}:
            cls._resolve_incident(
                connection,
                str(opened["incident_uuid"]),
                timestamp,
                "recovered" if state == "healthy" else "operator_disabled",
            )

    @classmethod
    def _resolve_incident(
        cls,
        connection: sqlite3.Connection,
        incident_uuid: str,
        timestamp: str,
        reason: str,
    ) -> None:
        incident = connection.execute(
            "SELECT camera_uuid, kind FROM camera_incidents WHERE incident_uuid = ?",
            (incident_uuid,),
        ).fetchone()
        if incident is None:
            return
        connection.execute(
            "UPDATE camera_incidents SET last_observed_at = ?, resolved_at = ?, "
            "resolution_reason = ? WHERE incident_uuid = ? AND resolved_at IS NULL",
            (timestamp, timestamp, reason, incident_uuid),
        )
        cls._enqueue_incident_notification(
            connection,
            incident_uuid,
            str(incident["camera_uuid"]),
            str(incident["kind"]),
            "incident_resolved",
            timestamp,
        )

    @staticmethod
    def _enqueue_incident_notification(
        connection: sqlite3.Connection,
        incident_uuid: str,
        camera_uuid: str,
        kind: str,
        event_type: str,
        timestamp: str,
    ) -> None:
        settings_row = connection.execute(
            "SELECT enabled, chat_id FROM notification_settings WHERE singleton_id = 1"
        ).fetchone()
        if settings_row is None or not bool(settings_row["enabled"]) or not settings_row["chat_id"]:
            return
        camera = connection.execute(
            "SELECT display_name FROM cameras WHERE camera_uuid = ?",
            (camera_uuid,),
        ).fetchone()
        if camera is None:
            return
        payload = json.dumps(
            {
                "camera_id": camera_uuid,
                "camera_name": str(camera["display_name"]),
                "kind": kind,
                "observed_at": timestamp,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "INSERT OR IGNORE INTO notification_outbox(outbox_uuid, incident_uuid, event_type, "
            "payload_json, idempotency_key, status, next_attempt_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                str(uuid.uuid4()),
                incident_uuid,
                event_type,
                payload,
                f"{incident_uuid}:{event_type}",
                timestamp,
                timestamp,
            ),
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
        buckets: list[dict[str, Any]] = []
        for position in range(bucket_count):
            bucket_start = window_start + timedelta(seconds=bucket_seconds * position)
            bucket_end = window_start + timedelta(seconds=bucket_seconds * (position + 1))
            overlapping = [
                segment
                for segment in segments
                if segment[0] < bucket_end and segment[1] > bucket_start
            ]
            bucket_segments = []
            for segment_start, segment_end, segment_state, segment_reason in overlapping:
                clipped_start = max(segment_start, bucket_start)
                clipped_end = min(segment_end, bucket_end)
                if clipped_end <= clipped_start:
                    continue
                bucket_segments.append(
                    {
                        "start": clipped_start.isoformat(),
                        "end": clipped_end.isoformat(),
                        "state": segment_state,
                        "reason": segment_reason,
                        "seconds": round((clipped_end - clipped_start).total_seconds(), 3),
                    }
                )
            ending_segment = bucket_segments[-1] if bucket_segments else None
            bucket_state = str(ending_segment["state"]) if ending_segment else "unknown"
            bucket_reason = str(ending_segment["reason"]) if ending_segment else "no_observation"
            buckets.append(
                {
                    "start": bucket_start.isoformat(),
                    "end": bucket_end.isoformat(),
                    "state": bucket_state,
                    "reason": bucket_reason,
                    "segments": bucket_segments,
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

    def incidents(self, *, status: str = "open", limit: int = 50) -> dict[str, Any]:
        filters = {
            "open": "i.resolved_at IS NULL",
            "resolved": "i.resolved_at IS NOT NULL",
            "all": "1 = 1",
        }
        where = filters.get(status)
        if where is None:
            raise ValueError("Incident status is invalid")
        bounded_limit = max(1, min(100, limit))
        with self.connect() as connection:
            open_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM camera_incidents WHERE resolved_at IS NULL"
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT i.incident_uuid AS id, i.camera_uuid AS camera_id, "
                "c.display_name AS camera_name, i.kind, i.severity, "
                "CASE WHEN i.resolved_at IS NULL THEN 'open' ELSE 'resolved' END AS status, "
                "i.opened_at, i.last_observed_at, i.resolved_at, i.resolution_reason "
                "FROM camera_incidents i JOIN cameras c USING(camera_uuid) "
                f"WHERE {where} ORDER BY i.opened_at DESC LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        return {"open_count": open_count, "incidents": [dict(row) for row in rows]}

    def notification_credentials(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM notification_settings WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        ciphertext = result.pop("bot_token_ciphertext")
        pairing_ciphertext = result.pop("pairing_token_ciphertext")
        result["enabled"] = bool(result["enabled"])
        result["bot_token"] = (
            decrypt_password(ciphertext, "notification-telegram-bot", self.master_key)
            if ciphertext is not None
            else None
        )
        result["pairing_token"] = (
            decrypt_password(pairing_ciphertext, "notification-telegram-pairing", self.master_key)
            if pairing_ciphertext is not None
            else None
        )
        return result

    def discovery_network_settings(self) -> dict[str, list[str]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT custom_subnets_json, excluded_detected_subnets_json, "
                "excluded_custom_subnets_json "
                "FROM discovery_settings WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            return {
                "custom_subnets": [],
                "excluded_detected_subnets": [],
                "excluded_custom_subnets": [],
            }

        def values(column: str) -> list[str]:
            try:
                parsed = json.loads(str(row[column]))
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
            if not isinstance(parsed, list):
                return []
            return [str(value) for value in parsed if isinstance(value, str)]

        return {
            "custom_subnets": values("custom_subnets_json"),
            "excluded_detected_subnets": values("excluded_detected_subnets_json"),
            "excluded_custom_subnets": values("excluded_custom_subnets_json"),
        }

    def save_discovery_network_settings(
        self,
        *,
        custom_subnets: list[str],
        excluded_detected_subnets: list[str],
        excluded_custom_subnets: list[str],
    ) -> None:
        timestamp = _now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO discovery_settings(singleton_id, custom_subnets_json, "
                "excluded_detected_subnets_json, excluded_custom_subnets_json, "
                "updated_at) VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(singleton_id) DO UPDATE SET "
                "custom_subnets_json=excluded.custom_subnets_json, "
                "excluded_detected_subnets_json=excluded.excluded_detected_subnets_json, "
                "excluded_custom_subnets_json=excluded.excluded_custom_subnets_json, "
                "updated_at=excluded.updated_at",
                (
                    json.dumps(custom_subnets, separators=(",", ":")),
                    json.dumps(excluded_detected_subnets, separators=(",", ":")),
                    json.dumps(excluded_custom_subnets, separators=(",", ":")),
                    timestamp,
                ),
            )
            connection.commit()

    def notification_settings(self) -> dict[str, Any]:
        credentials = self.notification_credentials()
        with self.connect() as connection:
            delivery = connection.execute(
                "SELECT status, sent_at, last_error_code, event_type FROM notification_outbox "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return {
            "provider": "telegram",
            "enabled": bool(credentials and credentials["enabled"]),
            "bot_configured": bool(credentials and credentials.get("bot_token")),
            "bot_username": credentials.get("bot_username") if credentials else None,
            "connection_status": (
                "connected"
                if credentials and credentials.get("chat_id")
                else "waiting_for_start"
                if credentials and credentials.get("bot_token")
                else "not_configured"
            ),
            "destination": credentials.get("chat_label") if credentials else None,
            "last_delivery": dict(delivery) if delivery is not None else None,
        }

    def save_telegram_settings(
        self,
        *,
        enabled: bool,
        bot_token: str | None,
        bot_id: str | None = None,
        bot_username: str | None = None,
        pairing_token: str | None = None,
        pairing_expires_at: str | None = None,
    ) -> None:
        timestamp = _now()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT bot_token_ciphertext, bot_id, bot_username, chat_id, chat_label, "
                "pairing_token_ciphertext, pairing_expires_at, update_offset, created_at "
                "FROM notification_settings WHERE singleton_id = 1"
            ).fetchone()
            replacing = bot_token is not None
            token_ciphertext = (
                encrypt_password(bot_token, "notification-telegram-bot", self.master_key)
                if replacing
                else existing["bot_token_ciphertext"] if existing is not None else None
            )
            pairing_ciphertext = (
                encrypt_password(pairing_token, "notification-telegram-pairing", self.master_key)
                if pairing_token is not None
                else existing["pairing_token_ciphertext"] if existing is not None else None
            )
            values = {
                "bot_id": bot_id if replacing else existing["bot_id"] if existing is not None else None,
                "bot_username": bot_username if replacing else existing["bot_username"] if existing is not None else None,
                "chat_id": None if replacing else existing["chat_id"] if existing is not None else None,
                "chat_label": None if replacing else existing["chat_label"] if existing is not None else None,
                "pairing_expires_at": pairing_expires_at if pairing_token is not None else existing["pairing_expires_at"] if existing is not None else None,
                "update_offset": None if replacing else existing["update_offset"] if existing is not None else None,
                "created_at": existing["created_at"] if existing is not None else timestamp,
            }
            connection.execute(
                "INSERT INTO notification_settings(singleton_id, provider, enabled, bot_token_ciphertext, "
                "bot_id, bot_username, chat_id, chat_label, pairing_token_ciphertext, "
                "pairing_expires_at, update_offset, created_at, updated_at) "
                "VALUES (1, 'telegram', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(singleton_id) DO UPDATE SET enabled=excluded.enabled, "
                "bot_token_ciphertext=excluded.bot_token_ciphertext, bot_id=excluded.bot_id, "
                "bot_username=excluded.bot_username, chat_id=excluded.chat_id, "
                "chat_label=excluded.chat_label, pairing_token_ciphertext=excluded.pairing_token_ciphertext, "
                "pairing_expires_at=excluded.pairing_expires_at, update_offset=excluded.update_offset, "
                "updated_at=excluded.updated_at",
                (
                    int(enabled), token_ciphertext, values["bot_id"], values["bot_username"],
                    values["chat_id"], values["chat_label"], pairing_ciphertext,
                    values["pairing_expires_at"], values["update_offset"], values["created_at"], timestamp,
                ),
            )
            connection.commit()

    def complete_telegram_pairing(
        self,
        *,
        chat_id: str,
        chat_label: str,
        update_offset: int,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE notification_settings SET chat_id=?, chat_label=?, "
                "pairing_token_ciphertext=NULL, pairing_expires_at=NULL, update_offset=?, updated_at=? "
                "WHERE singleton_id=1",
                (chat_id, chat_label, update_offset, _now()),
            )
            connection.commit()

    def update_telegram_offset(self, update_offset: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE notification_settings SET update_offset=?, updated_at=? WHERE singleton_id=1",
                (update_offset, _now()),
            )
            connection.commit()

    def enqueue_test_notification(self) -> dict[str, Any]:
        timestamp = _now()
        outbox_uuid = str(uuid.uuid4())
        payload = json.dumps(
            {"camera_name": "CamAdmiral", "kind": "test", "observed_at": timestamp},
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO notification_outbox(outbox_uuid, event_type, payload_json, "
                "idempotency_key, status, next_attempt_at, created_at) "
                "VALUES (?, 'test', ?, ?, 'pending', ?, ?)",
                (outbox_uuid, payload, f"test:{outbox_uuid}", timestamp, timestamp),
            )
            connection.commit()
        return self.outbox_item(outbox_uuid) or {}

    def outbox_item(self, outbox_uuid: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE outbox_uuid = ?",
                (outbox_uuid,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def due_notifications(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM notification_outbox WHERE status IN ('pending', 'retry') "
                "AND next_attempt_at <= ? ORDER BY created_at LIMIT ?",
                (_now(), max(1, min(100, limit))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def complete_notification_delivery(
        self,
        outbox_uuid: str,
        *,
        message_id: str,
    ) -> None:
        timestamp = _now()
        with self.connect() as connection:
            connection.execute(
                "UPDATE notification_outbox SET status='sent', attempt_count=attempt_count+1, "
                "provider_message_id=?, last_error_code=NULL, sent_at=? WHERE outbox_uuid=?",
                (message_id, timestamp, outbox_uuid),
            )
            connection.commit()

    def fail_notification_delivery(
        self,
        outbox_uuid: str,
        *,
        error_code: str,
        retry_after_seconds: int | None,
    ) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT attempt_count FROM notification_outbox WHERE outbox_uuid=?",
                (outbox_uuid,),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempt_count"]) + 1
            retry = retry_after_seconds is not None and attempts < 8
            delay = retry_after_seconds or min(3600, 30 * (2 ** max(0, attempts - 1)))
            next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            connection.execute(
                "UPDATE notification_outbox SET status=?, attempt_count=?, next_attempt_at=?, "
                "last_error_code=? WHERE outbox_uuid=?",
                ("retry" if retry else "failed", attempts, next_attempt, error_code, outbox_uuid),
            )
            connection.commit()

    def adoption_for_candidate(self, candidate_uuid: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            camera = connection.execute(
                "SELECT camera_uuid, candidate_uuid, display_name, credential_uuid, adopted_at, enabled, "
                "stream_address_mode "
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
                "stream_address_mode": str(camera["stream_address_mode"]),
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

    @staticmethod
    def _candidate_stable_identity(candidate: dict[str, Any]) -> tuple[str | None, str | None]:
        onvif = candidate.get("onvif") or {}
        onvif_identity = str(onvif.get("endpoint_reference") or "").strip().lower() or None
        mac = str(candidate.get("mac") or "").strip().lower() or None
        return onvif_identity, mac

    def blocked_devices(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT block_uuid, candidate_uuid, onvif_identity, mac, display_name, "
                "last_ip, blocked_at, updated_at FROM blocked_devices "
                "ORDER BY display_name COLLATE NOCASE, block_uuid"
            ).fetchall()
        return [dict(row) for row in rows]

    def blocked_device_for_candidate(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        onvif_identity, mac = self._candidate_stable_identity(candidate)
        clauses: list[str] = []
        values: list[str] = []
        if onvif_identity:
            clauses.append("onvif_identity = ?")
            values.append(onvif_identity)
        if mac:
            clauses.append("mac = ?")
            values.append(mac)
        if not clauses:
            return None
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT block_uuid, candidate_uuid, onvif_identity, mac, display_name, "
                f"last_ip, blocked_at, updated_at FROM blocked_devices WHERE {' OR '.join(clauses)}",
                values,
            ).fetchall()
        if len(rows) > 1:
            return None
        return dict(rows[0]) if rows else None

    def block_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        if candidate.get("identity_conflict"):
            raise ValueError("Camera has a conflicting stable identity")
        onvif_identity, mac = self._candidate_stable_identity(candidate)
        if not onvif_identity and not mac:
            raise ValueError("Camera has no stable ONVIF identity or MAC address")
        timestamp = _now()
        display_name = str(candidate.get("display_name") or candidate.get("ip") or "Blocked device")
        candidate_uuid = str(candidate.get("candidate_uuid") or "") or None
        last_ip = str(candidate.get("ip") or "") or None
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            clauses: list[str] = []
            values: list[str] = []
            if onvif_identity:
                clauses.append("onvif_identity = ?")
                values.append(onvif_identity)
            if mac:
                clauses.append("mac = ?")
                values.append(mac)
            matches = connection.execute(
                "SELECT block_uuid FROM blocked_devices WHERE " + " OR ".join(clauses),
                values,
            ).fetchall()
            if len(matches) > 1:
                connection.rollback()
                raise ValueError("ONVIF identity and MAC match different blocked devices")
            block_uuid = str(matches[0]["block_uuid"]) if matches else str(uuid.uuid4())
            connection.execute(
                "INSERT INTO blocked_devices(block_uuid, candidate_uuid, onvif_identity, mac, "
                "display_name, last_ip, blocked_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(block_uuid) DO UPDATE SET candidate_uuid=excluded.candidate_uuid, "
                "onvif_identity=COALESCE(excluded.onvif_identity, blocked_devices.onvif_identity), "
                "mac=COALESCE(excluded.mac, blocked_devices.mac), "
                "display_name=excluded.display_name, last_ip=excluded.last_ip, updated_at=excluded.updated_at",
                (
                    block_uuid,
                    candidate_uuid,
                    onvif_identity,
                    mac,
                    display_name,
                    last_ip,
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        blocked = self.blocked_device_for_candidate(candidate)
        assert blocked is not None
        return blocked

    def unblock_device(self, block_uuid: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM blocked_devices WHERE block_uuid = ?",
                (block_uuid,),
            )
            connection.commit()
        return cursor.rowcount == 1

    def unadopt_camera(self, camera_uuid: str) -> bool:
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
            connection.execute("DELETE FROM cameras WHERE camera_uuid = ?", (camera_uuid,))
            connection.execute(
                "DELETE FROM camera_credentials WHERE credential_uuid = ?",
                (credential_uuid,),
            )
            connection.commit()
        return True

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
                    "SELECT s.stream_uuid, s.stream_key, s.probe_status, s.health_status, "
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

    def set_camera_stream_address_mode(self, camera_uuid: str, address_mode: str) -> bool:
        if address_mode not in {"lan", "localhost"}:
            raise ValueError("Camera stream address mode is invalid")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE cameras SET stream_address_mode = ?, updated_at = ? WHERE camera_uuid = ?",
                (address_mode, _now(), camera_uuid),
            )
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

    def frigate_targets(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT t.target_id, t.name, t.api_url, t.connection_status, "
                "t.last_error_code, t.last_checked_at, t.restart_recommended, "
                "t.address_mode, "
                "t.created_at, t.updated_at, "
                "COUNT(s.camera_uuid) AS selected_cameras "
                "FROM frigate_targets t LEFT JOIN frigate_camera_selections s "
                "ON s.target_id = t.target_id GROUP BY t.target_id "
                "ORDER BY t.name COLLATE NOCASE, t.target_id"
            ).fetchall()
        targets = [dict(row) for row in rows]
        for target in targets:
            target["restart_recommended"] = bool(target["restart_recommended"])
        return targets

    def save_frigate_target(
        self,
        target_id: str,
        name: str,
        api_url: str,
    ) -> None:
        timestamp = _now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO frigate_targets(target_id, name, api_url, sync_cameras, "
                "connection_status, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, 'pending', ?, ?) "
                "ON CONFLICT(target_id) DO UPDATE SET name=excluded.name, "
                "api_url=excluded.api_url, "
                "connection_status=CASE WHEN frigate_targets.api_url != excluded.api_url "
                "THEN 'pending' ELSE frigate_targets.connection_status END, "
                "last_error_code=CASE WHEN frigate_targets.api_url != excluded.api_url "
                "THEN NULL ELSE frigate_targets.last_error_code END, "
                "last_checked_at=CASE WHEN frigate_targets.api_url != excluded.api_url "
                "THEN NULL ELSE frigate_targets.last_checked_at END, "
                "restart_recommended=CASE WHEN frigate_targets.api_url != excluded.api_url "
                "THEN 0 ELSE frigate_targets.restart_recommended END, "
                "updated_at=excluded.updated_at",
                (target_id, name, api_url, timestamp, timestamp),
            )
            connection.commit()

    def frigate_target(self, target_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT t.target_id, t.name, t.api_url, t.connection_status, "
                "t.last_error_code, t.last_checked_at, t.restart_recommended, "
                "t.address_mode, "
                "t.created_at, t.updated_at, "
                "COUNT(s.camera_uuid) AS selected_cameras "
                "FROM frigate_targets t LEFT JOIN frigate_camera_selections s "
                "ON s.target_id = t.target_id WHERE t.target_id = ? GROUP BY t.target_id",
                (target_id,),
            ).fetchone()
        if row is None:
            return None
        target = dict(row)
        target["restart_recommended"] = bool(target["restart_recommended"])
        return target

    def set_frigate_target_address_mode(
        self,
        target_id: str,
        address_mode: str,
    ) -> bool:
        if address_mode not in {"lan", "localhost"}:
            raise ValueError("Frigate address mode is invalid")
        timestamp = _now()
        with self.connect() as connection:
            current = connection.execute(
                "SELECT address_mode FROM frigate_targets WHERE target_id = ?",
                (target_id,),
            ).fetchone()
            if current is None:
                return False
            connection.execute(
                "UPDATE frigate_targets SET address_mode = ?, updated_at = ? "
                "WHERE target_id = ?",
                (address_mode, timestamp, target_id),
            )
            connection.execute(
                "UPDATE frigate_camera_selections SET address_mode = ? WHERE target_id = ?",
                (address_mode, target_id),
            )
            connection.commit()
        return current["address_mode"] != address_mode

    def select_frigate_camera(
        self,
        target_id: str,
        camera_uuid: str,
        address_mode: str = "lan",
    ) -> bool:
        if address_mode not in {"lan", "localhost"}:
            raise ValueError("Frigate address mode is invalid")
        with self.connect() as connection:
            target = connection.execute(
                "SELECT address_mode FROM frigate_targets WHERE target_id = ?",
                (target_id,),
            ).fetchone()
            if target is not None and target["address_mode"] is not None:
                address_mode = str(target["address_mode"])
            current = connection.execute(
                "SELECT address_mode FROM frigate_camera_selections "
                "WHERE target_id = ? AND camera_uuid = ?",
                (target_id, camera_uuid),
            ).fetchone()
            connection.execute(
                "INSERT INTO frigate_camera_selections"
                "(target_id, camera_uuid, selected_at, address_mode) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(target_id, camera_uuid) DO UPDATE SET "
                "address_mode=excluded.address_mode",
                (target_id, camera_uuid, _now(), address_mode),
            )
            connection.commit()
        return current is None or str(current["address_mode"]) != address_mode

    def deselect_frigate_camera(self, target_id: str, camera_uuid: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM frigate_camera_selections WHERE target_id = ? AND camera_uuid = ?",
                (target_id, camera_uuid),
            )
            connection.commit()
        return cursor.rowcount == 1

    def selected_frigate_camera_uuids(self, target_id: str) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT camera_uuid FROM frigate_camera_selections "
                "WHERE target_id = ? ORDER BY selected_at, camera_uuid",
                (target_id,),
            ).fetchall()
        return [str(row["camera_uuid"]) for row in rows]

    def frigate_camera_selections(self, target_id: str) -> list[dict[str, str]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT camera_uuid, address_mode FROM frigate_camera_selections "
                "WHERE target_id = ? ORDER BY selected_at, camera_uuid",
                (target_id,),
            ).fetchall()
        return [
            {
                "camera_uuid": str(row["camera_uuid"]),
                "address_mode": str(row["address_mode"]),
            }
            for row in rows
        ]

    def frigate_camera_address_mode(
        self,
        target_id: str,
        camera_uuid: str,
    ) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT address_mode FROM frigate_camera_selections "
                "WHERE target_id = ? AND camera_uuid = ?",
                (target_id, camera_uuid),
            ).fetchone()
        return str(row["address_mode"]) if row is not None else None

    def remove_frigate_target(self, target_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM frigate_targets WHERE target_id = ?",
                (target_id,),
            )
            connection.commit()
        return cursor.rowcount == 1

    def record_frigate_target_check(
        self,
        target_id: str,
        *,
        status: str,
        error_code: str | None = None,
        restart_recommended: bool | None = None,
    ) -> None:
        if status not in {"connected", "error"}:
            raise ValueError("Frigate target status is invalid")
        timestamp = _now()
        with self.connect() as connection:
            if restart_recommended is None:
                connection.execute(
                    "UPDATE frigate_targets SET connection_status=?, last_error_code=?, "
                    "last_checked_at=?, updated_at=? WHERE target_id=?",
                    (status, error_code, timestamp, timestamp, target_id),
                )
            else:
                connection.execute(
                    "UPDATE frigate_targets SET connection_status=?, last_error_code=?, "
                    "restart_recommended=?, last_checked_at=?, updated_at=? WHERE target_id=?",
                    (
                        status,
                        error_code,
                        int(restart_recommended),
                        timestamp,
                        timestamp,
                        target_id,
                    ),
                )
            connection.commit()

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

    def mark_frigate_binding_pending(self, target_id: str, camera_uuid: str) -> None:
        timestamp = _now()
        with self.connect() as connection:
            connection.execute(
                "UPDATE frigate_bindings SET status='pending', last_error_code=NULL, "
                "last_attempt_at=?, updated_at=? WHERE target_id=? AND camera_uuid=?",
                (timestamp, timestamp, target_id, camera_uuid),
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

    def remove_frigate_binding(self, target_id: str, camera_uuid: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM frigate_bindings WHERE target_id = ? AND camera_uuid = ?",
                (target_id, camera_uuid),
            )
            connection.commit()
        return cursor.rowcount == 1

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
        bound_role: str | None = None,
    ) -> list[dict[str, str]]:
        health_filter = "" if include_auth_failed else "AND s.health_status != 'auth_failed' "
        enabled_filter = "" if include_disabled else "AND a.enabled = 1 "
        camera_filter = "AND s.camera_uuid = ? " if camera_uuid is not None else ""
        role_join = (
            "JOIN consumer_bindings b ON b.stream_uuid = s.stream_uuid "
            if role_bound_only or bound_role is not None
            else ""
        )
        role_filter = "AND b.role = ? " if bound_role is not None else ""
        parameters = (
            *((bound_role,) if bound_role is not None else ()),
            *((camera_uuid,) if camera_uuid is not None else ()),
        )
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT s.stream_uuid, s.stream_key, s.camera_uuid, p.uri, p.source_scheme, p.source_host, p.source_port, "
                "p.source_path, p.source_query, c.username, c.password_ciphertext, c.credential_uuid "
                "FROM managed_streams s JOIN onvif_profiles p USING (profile_uuid) "
                "JOIN cameras a USING (camera_uuid) JOIN camera_credentials c USING (credential_uuid) "
                f"{role_join}WHERE p.uri IS NOT NULL {health_filter}{enabled_filter}{role_filter}{camera_filter}"
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

    def managed_stream_runtime_sources(self) -> list[dict[str, str]]:
        """Return role-bound stream identities without loading camera credentials."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT s.stream_uuid, s.stream_key, s.camera_uuid "
                "FROM managed_streams s "
                "JOIN onvif_profiles p USING (profile_uuid) "
                "JOIN cameras a USING (camera_uuid) "
                "JOIN consumer_bindings b ON b.stream_uuid = s.stream_uuid "
                "WHERE p.uri IS NOT NULL AND a.enabled = 1 "
                "AND s.health_status != 'auth_failed' "
                "ORDER BY s.camera_uuid, s.stream_key"
            ).fetchall()
        return [
            {
                "stream_uuid": str(row["stream_uuid"]),
                "stream_key": str(row["stream_key"]),
                "camera_uuid": str(row["camera_uuid"]),
            }
            for row in rows
        ]

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
                    "SELECT camera_uuid, consecutive_failures, health_status "
                    "FROM managed_streams WHERE stream_uuid = ?",
                    (stream_uuid,),
                ).fetchone()
                if current is None:
                    continue
                affected_cameras.add(str(current["camera_uuid"]))
                failures = 0 if result.status in {"ready", "idle"} else int(current["consecutive_failures"]) + 1
                if result.status == "ready":
                    health_status = "healthy"
                elif result.status == "idle":
                    health_status = "unknown"
                elif result.status == "auth_failed":
                    health_status = "auth_failed"
                elif failures >= 3:
                    health_status = "offline"
                elif failures >= 2:
                    health_status = "degraded"
                else:
                    health_status = (
                        "healthy" if current["health_status"] == "healthy" else "unknown"
                    )
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
                        "UPDATE managed_streams SET video_codec=COALESCE(?, video_codec), "
                        "audio_codec=COALESCE(?, audio_codec), "
                        "probed_width=CASE WHEN ?>0 THEN ? ELSE probed_width END, "
                        "probed_height=CASE WHEN ?>0 THEN ? ELSE probed_height END, "
                        "probed_fps=CASE WHEN ?>0 THEN ? ELSE probed_fps END WHERE stream_uuid=?",
                        (
                            result.video_codec,
                            result.audio_codec,
                            result.width,
                            result.width,
                            result.height,
                            result.height,
                            result.fps,
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
