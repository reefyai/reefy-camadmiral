"""Bounded recovery when a live relay transport stops delivering video."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
import uuid
from typing import Any

from .media import (
    ProbeResult, RelayHealthMonitor, SnapshotBusyError, SnapshotError,
    _request, restart_relay, snapshot_frame,
)

# These are internal safety limits, not operator-configurable API fields.
STALL_SECONDS = 20.0
RESTART_COOLDOWN = 120.0
RESTART_WINDOW = 3600.0
MAX_RESTARTS = 3


def event(name: str, **fields: Any) -> None:
    # Callers pass identifiers and numeric counters only. Never log source URLs,
    # credentials, exception text, SDP or camera-provided names.
    print('media-watchdog: ' + json.dumps({'event': name, 'at': datetime.now(timezone.utc).isoformat(), **fields}, sort_keys=True), flush=True)


class StalledVideoWatchdog:
    def __init__(self) -> None:
        self._samples: dict[str, dict[str, Any]] = {}
        self._deferred: tuple[Any, ...] | None = None
        self._verification: dict[str, dict[str, Any]] = {}

    def _reserve_restart(self, path: Path, now: float, *, reserve: bool = True) -> tuple[bool, float]:
        try:
            raw = path.read_bytes()
            if len(raw) > 4096:
                raise ValueError('invalid budget')
            saved = json.loads(raw)
            if not isinstance(saved, dict) or saved.get('version') != 1 or not isinstance(saved.get('attempts'), list):
                raise ValueError('invalid budget')
            attempts = saved['attempts']
            if any(isinstance(t, bool) or not isinstance(t, (float, int))
                   or not math.isfinite(t) or t < 0 for t in attempts):
                raise ValueError('invalid budget')
        except FileNotFoundError:
            attempts = []
        attempts = sorted(t for t in attempts if now - t < RESTART_WINDOW)
        wait = max(0.0, (attempts[-1] + RESTART_COOLDOWN - now) if attempts else 0.0)
        if len(attempts) >= MAX_RESTARTS:
            wait = max(wait, attempts[-MAX_RESTARTS] + RESTART_WINDOW - now)
        if wait:
            return False, wait
        if not reserve:
            return True, 0.0
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump({'version': 1, 'attempts': attempts + [now]}, output)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return True, 0.0

    def cycle(self, repository: Any) -> bool:
        sources = repository.managed_stream_runtime_sources()
        active = {source['stream_uuid'] for source in sources}
        self._samples = {key: value for key, value in self._samples.items() if key in active}
        if self._verify_pending(repository, {s['stream_uuid']: s for s in sources}):
            return False
        if not sources:
            return False
        runtime = json.loads(_request('GET', '/api/streams'))
        if not isinstance(runtime, dict):
            raise ValueError('invalid relay state')
        now = time.monotonic()
        candidates = []
        for source in sources:
            uid = source['stream_uuid']
            stream = runtime.get(source['stream_key'])
            if not RelayHealthMonitor._has_external_consumers(stream):
                self._samples.pop(uid, None)
                continue
            producers = stream.get('producers') or []
            generation = tuple(str(p.get('id')) for p in producers)
            video = sum(int(receiver.get('packets') or 0) for p in producers
                        for receiver in p.get('receivers') or []
                        if (receiver.get('codec') or {}).get('codec_type') == 'video')
            audio = sum(int(receiver.get('packets') or 0) for p in producers
                        for receiver in p.get('receivers') or []
                        if (receiver.get('codec') or {}).get('codec_type') == 'audio')
            transport = sum(int(p.get('bytes_recv') or 0) for p in producers)
            previous = self._samples.get(uid)
            identity = (source['camera_uuid'], source['stream_key'], generation)
            if previous is None or previous['identity'] != identity or video > previous['video']:
                if previous and previous.get('reported'):
                    event('video_packets_resumed', stream_uuid=uid, video_packets=video,
                          producer_ids=list(generation))
                self._samples[uid] = dict(identity=identity, video=video, transport=transport,
                                         last_video=now, last_transport=now, reported=False)
                continue
            if transport > previous['transport']:
                previous['last_transport'] = now
            previous.update(video=video, transport=transport)
            stalled_for = now - previous['last_video']
            if stalled_for < STALL_SECONDS:
                continue
            traffic_recent = now - previous['last_transport'] < 10 and transport > 0
            counters = dict(camera_uuid=source['camera_uuid'], stream_uuid=uid,
                            producer_ids=list(generation), video_packets=video,
                            audio_packets=audio, transport_bytes=transport,
                            stalled_seconds=round(stalled_for, 1), transport_advancing=traffic_recent)
            if not previous['reported'] or now - previous.get('last_report', 0) >= 60:
                event('video_stalled', **counters)
                previous.update(reported=True, last_report=now)
            # A completely silent camera already gets go2rtc's connection retry.
            # Do not reset every healthy camera because one camera is powered off.
            if traffic_recent:
                candidates.append((source, previous, counters))
        if not candidates:
            self._deferred = None
            return False
        source, sample, counters = candidates[0]
        uid = source['stream_uuid']
        budget = repository.path.parent / 'relay-watchdog.json'
        try:
            allowed, wait = self._reserve_restart(budget, time.time(), reserve=False)
        except (OSError, ValueError, TypeError, KeyError):
            self._defer('budget_unavailable', uid)
            return False
        if not allowed:
            self._defer('restart_budget', uid, retry_after_seconds=round(wait))
            return False
        password = repository.rtsp_access_password()
        try:
            snapshot_frame(source['stream_key'], rtsp_password=password, width=320)
        except SnapshotBusyError:
            self._defer('snapshot_capacity', uid)
            return False
        except SnapshotError:
            pass
        else:
            # Counter anomalies alone are insufficient reason to reset a relay.
            sample['last_video'] = time.monotonic()
            sample['reported'] = False
            event('decoded_without_restart', stream_uuid=uid)
            return False
        try:
            allowed, wait = self._reserve_restart(budget, time.time())
        except (OSError, ValueError, TypeError, KeyError):
            self._defer('budget_unavailable', uid)
            return False
        if not allowed:
            self._defer('restart_budget', uid, retry_after_seconds=round(wait))
            return False
        self._deferred = None
        event('restart_started', **counters, scope='shared_relay')
        try:
            # Keep configured sources, IDs, roles, credentials and data intact.
            # The stock API cannot reliably close one attached producer.
            restart_relay()
        except Exception as exc:
            event('restart_failed', stream_uuid=uid, error_type=type(exc).__name__)
            return False
        try:
            repository.enqueue_relay_restart_notification(reason='stalled_video_recovery', camera_count=len({s['camera_uuid'] for s,_,_ in candidates}))
        except Exception as exc:
            event('notification_failed', error_type=type(exc).__name__)
        self._samples.clear()
        self._verification = {
            item['stream_uuid']: {'source': dict(item), 'deadline': time.monotonic() + 90,
                                  'attempts': 0, 'next_try': 0.0}
            for item, _, _ in candidates
        }
        self._verify_pending(repository, {s['stream_uuid']: s for s in sources})
        return True

    def _verify_pending(self, repository: Any, current: dict[str, dict[str, str]]) -> bool:
        now = time.monotonic()
        for uid, pending in list(self._verification.items()):
            if current.get(uid) != pending['source']:
                event('verification_cancelled', stream_uuid=uid, reason='source_changed_or_disabled')
                del self._verification[uid]
            elif now >= pending['deadline']:
                event('verification_failed', stream_uuid=uid, reason='deadline_expired')
                del self._verification[uid]
        if not self._verification:
            return False
        uid, pending = next(iter(self._verification.items()))
        if pending['next_try'] > now:
            return True
        try:
            snapshot_frame(pending['source']['stream_key'], rtsp_password=repository.rtsp_access_password(), width=320)
        except SnapshotBusyError:
            pending['next_try'] = time.monotonic() + 5
            event('verification_deferred', stream_uuid=uid, reason='snapshot_capacity')
        except SnapshotError:
            pending['attempts'] += 1
            pending['next_try'] = time.monotonic() + 10
            event('verification_failed', stream_uuid=uid, reason='no_decoded_video', attempt=pending['attempts'])
            if pending['attempts'] >= 3:
                del self._verification[uid]
        else:
            repository.record_probe_results({uid: ProbeResult('ready', 0)})
            event('recovery_verified', stream_uuid=uid, evidence='decoded_frame')
            del self._verification[uid]
        return True

    def _defer(self, reason: str, uid: str, **fields: Any) -> None:
        state = (reason, uid)
        if state != self._deferred:
            event('recovery_deferred', stream_uuid=uid, reason=reason, **fields)
        self._deferred = state
