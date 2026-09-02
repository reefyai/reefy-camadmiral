from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any


GO2RTC_URL = os.environ.get("CAMADMIRAL_GO2RTC_URL", "http://127.0.0.1:1984").rstrip("/")
GO2RTC_RTSP_URL = os.environ.get("CAMADMIRAL_GO2RTC_RTSP_URL", "rtsp://127.0.0.1:18554").rstrip("/")
PROBE_TIMEOUT = float(os.environ.get("CAMADMIRAL_MEDIA_PROBE_TIMEOUT", "8"))
PROBE_WORKERS = int(os.environ.get("CAMADMIRAL_MEDIA_PROBE_WORKERS", "4"))
SNAPSHOT_TIMEOUT = max(1.0, float(os.environ.get("CAMADMIRAL_SNAPSHOT_TIMEOUT", "5")))
SNAPSHOT_MAX_BYTES = max(
    1024,
    int(os.environ.get("CAMADMIRAL_SNAPSHOT_MAX_BYTES", str(8 * 1024 * 1024))),
)
FRAME_PROBE_INTERVAL = max(
    30.0,
    float(os.environ.get("CAMADMIRAL_FRAME_PROBE_INTERVAL", "60")),
)
FRAME_PROBE_BATCH_SIZE = max(
    1,
    int(os.environ.get("CAMADMIRAL_FRAME_PROBE_BATCH_SIZE", "8")),
)
THUMBNAIL_WIDTH = max(
    160,
    min(640, int(os.environ.get("CAMADMIRAL_THUMBNAIL_WIDTH", "320"))),
)
THUMBNAIL_CACHE_ITEMS = max(
    1,
    int(os.environ.get("CAMADMIRAL_THUMBNAIL_CACHE_ITEMS", "64")),
)
THUMBNAIL_CACHE_MAX_BYTES = max(
    1024,
    int(os.environ.get("CAMADMIRAL_THUMBNAIL_CACHE_MAX_BYTES", str(32 * 1024 * 1024))),
)


class SnapshotError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    status: str
    latency_ms: int
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int = 0
    height: int = 0
    fps: float = 0


@dataclass(frozen=True)
class CachedFrame:
    content: bytes
    captured_at: float


class RelayHealthMonitor:
    """Observe active relays and periodically sample one frame per camera."""

    def __init__(
        self,
        *,
        frame_probe_interval: float = FRAME_PROBE_INTERVAL,
        frame_probe_batch_size: int = FRAME_PROBE_BATCH_SIZE,
        thumbnail_width: int = THUMBNAIL_WIDTH,
        thumbnail_cache_items: int = THUMBNAIL_CACHE_ITEMS,
        thumbnail_cache_max_bytes: int = THUMBNAIL_CACHE_MAX_BYTES,
    ) -> None:
        self._video_samples: dict[str, tuple[str, int]] = {}
        self._failure_samples: dict[str, int] = {}
        self._frame_probe_attempts: dict[str, float] = {}
        self._frame_probe_retries: set[str] = set()
        self._frame_probe_interval = max(0.0, frame_probe_interval)
        self._frame_probe_batch_size = max(1, frame_probe_batch_size)
        self._thumbnail_width = max(1, thumbnail_width)
        self._thumbnail_cache_items = max(1, thumbnail_cache_items)
        self._thumbnail_cache_max_bytes = max(1, thumbnail_cache_max_bytes)
        self._frames: dict[str, CachedFrame] = {}
        self._frame_lock = threading.Lock()

    def cache_frame(self, camera_uuid: str, content: bytes) -> CachedFrame:
        frame = CachedFrame(content=content, captured_at=time.time())
        with self._frame_lock:
            if len(content) > self._thumbnail_cache_max_bytes:
                self._frames.pop(camera_uuid, None)
                return frame
            self._frames[camera_uuid] = frame
            while (
                len(self._frames) > self._thumbnail_cache_items
                or sum(len(item.content) for item in self._frames.values())
                > self._thumbnail_cache_max_bytes
            ):
                oldest = min(self._frames, key=lambda key: self._frames[key].captured_at)
                self._frames.pop(oldest, None)
        return frame

    def cached_frame(self, camera_uuid: str) -> CachedFrame | None:
        with self._frame_lock:
            return self._frames.get(camera_uuid)

    def _sample_periodic_sources(
        self,
        sources: list[dict[str, str]],
        preferred_sources: list[dict[str, str]],
    ) -> dict[str, ProbeResult]:
        now = time.monotonic()
        preferred = {str(source["camera_uuid"]): source for source in preferred_sources}
        for source in sources:
            preferred.setdefault(str(source["camera_uuid"]), source)
        due = []
        for camera_uuid, source in preferred.items():
            previous = self._frame_probe_attempts.get(camera_uuid)
            if (
                previous is None
                or camera_uuid in self._frame_probe_retries
                or now - previous >= self._frame_probe_interval
            ):
                due.append(source)
        due.sort(key=lambda source: (
            str(source["camera_uuid"]) in self._frame_probe_attempts,
            self._frame_probe_attempts.get(str(source["camera_uuid"]), -1.0),
        ))
        due = due[: self._frame_probe_batch_size]
        for source in due:
            camera_uuid = str(source["camera_uuid"])
            self._frame_probe_attempts[camera_uuid] = now
            self._frame_probe_retries.discard(camera_uuid)
        if not due:
            return {}

        results: dict[str, ProbeResult] = {}
        with ThreadPoolExecutor(max_workers=min(PROBE_WORKERS, len(due))) as executor:
            pending = {
                executor.submit(snapshot_frame, source["stream_key"], width=self._thumbnail_width): source
                for source in due
            }
            submitted_at = {future: time.monotonic() for future in pending}
            for future in as_completed(pending):
                source = pending[future]
                started = submitted_at[future]
                try:
                    content = future.result()
                    self.cache_frame(str(source["camera_uuid"]), content)
                    results[str(source["stream_uuid"])] = ProbeResult(
                        "ready",
                        round((time.monotonic() - started) * 1000),
                    )
                except Exception:
                    results[str(source["stream_uuid"])] = ProbeResult(
                        "unavailable",
                        round((time.monotonic() - started) * 1000),
                    )
        return results

    @staticmethod
    def _active_video_sample(stream: object) -> tuple[str, int] | None:
        if not isinstance(stream, dict):
            return None
        for producer in stream.get("producers") or []:
            if not isinstance(producer, dict) or "bytes_recv" not in producer:
                continue
            packets = 0
            found_video = False
            for receiver in producer.get("receivers") or []:
                if not isinstance(receiver, dict):
                    continue
                codec = receiver.get("codec")
                if not isinstance(codec, dict) or codec.get("codec_type") != "video":
                    continue
                found_video = True
                packets += int(receiver.get("packets") or 0)
            if found_video:
                return str(producer.get("id") or "producer"), packets
        return None

    def probe(self, repository: Any) -> dict[str, ProbeResult]:
        started = time.monotonic()
        sources = repository.managed_stream_sources(
            include_auth_failed=False,
            role_bound_only=True,
        )
        preferred_sources = repository.managed_stream_sources(
            include_auth_failed=False,
            bound_role="detect",
        )
        try:
            runtime = json.loads(_request("GET", "/api/streams"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("go2rtc returned invalid stream state") from exc
        if not isinstance(runtime, dict):
            raise RuntimeError("go2rtc returned invalid stream state")
        latency_ms = round((time.monotonic() - started) * 1000)
        active_ids = {str(source["stream_uuid"]) for source in sources}
        results: dict[str, ProbeResult] = {}
        auth_failures: dict[str, ProbeResult] = {}
        diagnostic_sources: dict[str, dict[str, str]] = {}
        for source in sources:
            stream_uuid = str(source["stream_uuid"])
            stream = runtime.get(source["stream_key"])
            consumers = stream.get("consumers") if isinstance(stream, dict) else None
            if not consumers:
                self._video_samples.pop(stream_uuid, None)
                continue
            else:
                current = self._active_video_sample(stream)
                if current is None or current[1] <= 0:
                    self._video_samples.pop(stream_uuid, None)
                    result = ProbeResult("unavailable", latency_ms)
                else:
                    previous = self._video_samples.get(stream_uuid)
                    self._video_samples[stream_uuid] = current
                    advancing = (
                        previous is None
                        or previous[0] != current[0]
                        or current[1] > previous[1]
                    )
                    result = ProbeResult("ready" if advancing else "unavailable", latency_ms)
            results[stream_uuid] = result
        source_by_stream = {str(source["stream_uuid"]): source for source in sources}
        sources_by_camera: dict[str, list[dict[str, str]]] = {}
        for source in sources:
            sources_by_camera.setdefault(str(source["camera_uuid"]), []).append(source)
        active_results = dict(results)
        periodic_results = self._sample_periodic_sources(sources, preferred_sources)
        for stream_uuid, result in periodic_results.items():
            camera_uuid = str(source_by_stream[stream_uuid]["camera_uuid"])
            camera_sources = sources_by_camera[camera_uuid]
            active_ready = any(
                active_results.get(str(source["stream_uuid"]), ProbeResult("idle", 0)).status
                == "ready"
                for source in camera_sources
            )
            if result.status != "ready" and not active_ready:
                self._frame_probe_retries.add(camera_uuid)
            for source in camera_sources:
                target_stream_uuid = str(source["stream_uuid"])
                active_result = active_results.get(target_stream_uuid)
                if active_result is None:
                    results[target_stream_uuid] = result
                elif target_stream_uuid == stream_uuid and result.status == "ready":
                    results[target_stream_uuid] = result
        for stream_uuid, result in results.items():
            source = source_by_stream[stream_uuid]
            if result.status == "unavailable":
                failures = self._failure_samples.get(stream_uuid, 0) + 1
                self._failure_samples[stream_uuid] = failures
                if failures == 2:
                    diagnostic_sources.setdefault(str(source["camera_uuid"]), source)
            else:
                self._failure_samples.pop(stream_uuid, None)
        if diagnostic_sources:
            with ThreadPoolExecutor(
                max_workers=min(PROBE_WORKERS, len(diagnostic_sources))
            ) as executor:
                diagnostics = {
                    executor.submit(
                        probe_source,
                        source["uri"],
                        source["username"],
                        source["password"],
                    ): camera_uuid
                    for camera_uuid, source in diagnostic_sources.items()
                }
                for future in as_completed(diagnostics):
                    camera_uuid = diagnostics[future]
                    try:
                        diagnostic = future.result()
                    except Exception:
                        continue
                    if diagnostic.status == "auth_failed":
                        auth_failures[camera_uuid] = diagnostic
        for stream_uuid in set(self._video_samples) - active_ids:
            self._video_samples.pop(stream_uuid, None)
        for stream_uuid in set(self._failure_samples) - active_ids:
            self._failure_samples.pop(stream_uuid, None)
        active_camera_ids = {str(source["camera_uuid"]) for source in sources}
        for camera_uuid in set(self._frame_probe_attempts) - active_camera_ids:
            self._frame_probe_attempts.pop(camera_uuid, None)
            self._frame_probe_retries.discard(camera_uuid)
            with self._frame_lock:
                self._frames.pop(camera_uuid, None)
        failed_camera_ids = set(auth_failures)
        repository.record_probe_results({
            stream_uuid: result
            for stream_uuid, result in results.items()
            if str(source_by_stream[stream_uuid]["camera_uuid"]) not in failed_camera_ids
        })
        for camera_uuid, result in auth_failures.items():
            repository.record_camera_auth_failure(camera_uuid, result)
        return results


class RelayRuntimeActivityMonitor:
    """Detect stalled relays without running camera or snapshot probes."""

    def __init__(self, *, stall_threshold: float = 5.0) -> None:
        self._stall_threshold = max(0.0, stall_threshold)
        self._samples: dict[str, tuple[str, int, float]] = {}
        self._stalled_camera_uuids: set[str] = set()

    @property
    def stalled_camera_uuids(self) -> set[str]:
        return set(self._stalled_camera_uuids)

    def reset(self) -> None:
        self._samples.clear()
        self._stalled_camera_uuids.clear()

    def poll(self, repository: Any) -> set[str]:
        sources = repository.managed_stream_runtime_sources()
        if not sources:
            self.reset()
            return set()
        try:
            runtime = json.loads(_request("GET", "/api/streams"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("go2rtc returned invalid stream state") from exc
        if not isinstance(runtime, dict):
            raise RuntimeError("go2rtc returned invalid stream state")

        now = time.monotonic()
        active_stream_uuids: set[str] = set()
        stalled_camera_uuids: set[str] = set()
        for source in sources:
            stream = runtime.get(source["stream_key"])
            consumers = stream.get("consumers") if isinstance(stream, dict) else None
            if not consumers:
                continue

            stream_uuid = str(source["stream_uuid"])
            camera_uuid = str(source["camera_uuid"])
            active_stream_uuids.add(stream_uuid)
            current = RelayHealthMonitor._active_video_sample(stream)
            previous = self._samples.get(stream_uuid)
            if current is not None and (
                previous is None
                or current[1] > previous[1]
                or (previous[0] != current[0] and current[1] > 0)
            ):
                last_advanced_at = now
            else:
                last_advanced_at = previous[2] if previous is not None else now

            producer_id, packets = (
                current
                if current is not None
                else previous[:2] if previous is not None else ("", 0)
            )
            self._samples[stream_uuid] = (producer_id, packets, last_advanced_at)
            if now - last_advanced_at >= self._stall_threshold:
                stalled_camera_uuids.add(camera_uuid)

        for stream_uuid in set(self._samples) - active_stream_uuids:
            self._samples.pop(stream_uuid, None)
        self._stalled_camera_uuids = stalled_camera_uuids
        return set(stalled_camera_uuids)


def authenticated_rtsp_uri(uri: str, username: str, password: str) -> str:
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
        raise ValueError("Unsupported media source URI")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    if username:
        userinfo = urllib.parse.quote(username, safe="")
        if password:
            userinfo += ":" + urllib.parse.quote(password, safe="")
        host = f"{userinfo}@{host}"
    return urllib.parse.urlunsplit(parsed._replace(netloc=host, fragment=""))


def _request(
    method: str,
    path: str,
    query: dict[str, str] | None = None,
    *,
    body: bytes | None = None,
) -> bytes:
    encoded = urllib.parse.urlencode(query or {})
    url = f"{GO2RTC_URL}{path}" + (f"?{encoded}" if encoded else "")
    request = urllib.request.Request(url, data=body, method=method)
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.read()


def preload_stream_keys() -> set[str]:
    try:
        preloads = json.loads(_request("GET", "/api/preload"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("go2rtc returned invalid preload state") from exc
    if not isinstance(preloads, dict):
        raise RuntimeError("go2rtc returned invalid preload state")
    return {str(name) for name in preloads}


def _start_preload(stream_key: str) -> bool:
    try:
        _request("PUT", "/api/preload", {"src": stream_key, "video": "all"})
        return True
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return False


def restart_preload(stream_key: str) -> bool:
    try:
        _request("DELETE", "/api/preload", {"src": stream_key})
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        pass
    return _start_preload(stream_key)


def reconcile_preloads(sources: list[dict[str, str]]) -> None:
    desired = {str(source["stream_key"]) for source in sources}
    current = preload_stream_keys()
    for stream_key in sorted(current - desired):
        if stream_key.startswith("stream_"):
            _request("DELETE", "/api/preload", {"src": stream_key})
    for stream_key in sorted(desired - current):
        _start_preload(stream_key)


def wait_for_go2rtc(attempts: int = 20, delay: float = 0.1) -> None:
    for attempt in range(attempts):
        try:
            _request("GET", "/api")
            return
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt + 1 == attempts:
                raise
            time.sleep(delay)


def reconcile_streams(sources: list[dict[str, str]]) -> None:
    wait_for_go2rtc()
    desired = {source["stream_key"] for source in sources}
    try:
        current = json.loads(_request("GET", "/api/streams"))
    except (ValueError, json.JSONDecodeError):
        current = {}
    for name in current:
        if name.startswith("stream_") and name not in desired:
            _request("DELETE", "/api/streams", {"src": name})
    for source in sources:
        uri = authenticated_rtsp_uri(source["uri"], source["username"], source["password"])
        _request("PUT", "/api/streams", {"name": source["stream_key"], "src": uri})


def runtime_stream_keys() -> set[str]:
    try:
        streams = json.loads(_request("GET", "/api/streams"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("go2rtc returned invalid stream state") from exc
    if not isinstance(streams, dict):
        raise RuntimeError("go2rtc returned invalid stream state")
    return {str(name) for name in streams}


def reconcile_runtime_drift(repository: Any) -> bool:
    sources = repository.managed_stream_sources()
    desired = {source["stream_key"] for source in sources}
    current = {
        stream_key
        for stream_key in runtime_stream_keys()
        if stream_key.startswith("stream_")
    }
    if current == desired:
        return False
    reconcile_and_probe(repository)
    return True


def snapshot_frame(stream_key: str, *, width: int = 960) -> bytes:
    if not stream_key.startswith("stream_") or not stream_key.removeprefix("stream_"):
        raise SnapshotError("Invalid managed stream")
    query = urllib.parse.urlencode({"src": stream_key, "width": str(max(1, width))})
    request = urllib.request.Request(
        f"{GO2RTC_URL}/api/frame.jpeg?{query}",
        headers={"Accept": "image/jpeg"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=SNAPSHOT_TIMEOUT) as response:
            content_type = response.headers.get_content_type()
            content_length = response.headers.get("Content-Length")
            if content_type != "image/jpeg":
                raise SnapshotError("Snapshot service returned an unexpected format")
            if content_length and int(content_length) > SNAPSHOT_MAX_BYTES:
                raise SnapshotError("Snapshot is too large")
            image = response.read(SNAPSHOT_MAX_BYTES + 1)
    except SnapshotError:
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise SnapshotError("Snapshot is unavailable") from exc
    if len(image) > SNAPSHOT_MAX_BYTES:
        raise SnapshotError("Snapshot is too large")
    if len(image) < 4 or not image.startswith(b"\xff\xd8\xff") or not image.endswith(b"\xff\xd9"):
        raise SnapshotError("Snapshot service returned invalid JPEG data")
    return image


def go2rtc_websocket_url(stream_key: str) -> str:
    if not stream_key.startswith("stream_") or not stream_key.removeprefix("stream_"):
        raise ValueError("Invalid managed stream")
    parsed = urllib.parse.urlsplit(GO2RTC_URL)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    query = urllib.parse.urlencode({"src": stream_key})
    return urllib.parse.urlunsplit((scheme, parsed.netloc, "/api/ws", query, ""))


def replace_streams(sources: list[dict[str, str]]) -> None:
    wait_for_go2rtc()
    persisted: dict[str, list[str]] = {}
    for source in sources:
        uri = authenticated_rtsp_uri(source["uri"], source["username"], source["password"])
        persisted[source["stream_key"]] = [uri]
    if not persisted:
        return

    # This PATCH writes only go2rtc's configuration file. It does not mutate an
    # existing producer. Restarting the stock binary then creates fresh stream
    # and producer objects, closes existing sessions, and lets clients reconnect
    # through their unchanged downstream URLs.
    body = json.dumps(
        {"streams": persisted},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _request("PATCH", "/api/config", body=body)
    try:
        _request("POST", "/api/restart")
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        # The restart can replace the process before the HTTP response flushes.
        pass
    time.sleep(0.1)
    wait_for_go2rtc(attempts=50, delay=0.1)


def _fps(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0
    try:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return 0


def _probe_uri(uri: str) -> ProbeResult:
    started = time.monotonic()
    command = [
        "ffprobe",
        "-v",
        "error",
        "-rtsp_transport",
        "tcp",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,avg_frame_rate",
        "-of",
        "json",
        uri,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult("timeout", round((time.monotonic() - started) * 1000))
    latency_ms = round((time.monotonic() - started) * 1000)
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="ignore").lower()
        if "401 unauthorized" in error or "403 forbidden" in error:
            return ProbeResult("auth_failed", latency_ms)
        return ProbeResult("unavailable", latency_ms)
    try:
        streams = json.loads(completed.stdout).get("streams", [])
    except (ValueError, json.JSONDecodeError):
        return ProbeResult("invalid_media", latency_ms)
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if video is None:
        return ProbeResult("no_video", latency_ms, audio_codec=audio.get("codec_name") if audio else None)
    return ProbeResult(
        "ready",
        latency_ms,
        video_codec=video.get("codec_name"),
        audio_codec=audio.get("codec_name") if audio else None,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=_fps(video.get("avg_frame_rate")),
    )


def probe_source(uri: str, username: str = "", password: str = "") -> ProbeResult:
    return _probe_uri(authenticated_rtsp_uri(uri, username, password))


def probe_stream(stream_key: str, username: str = "", password: str = "") -> ProbeResult:
    return _probe_uri(
        authenticated_rtsp_uri(f"{GO2RTC_RTSP_URL}/{stream_key}", username, password)
    )


def probe_streams(
    sources: list[dict[str, str]],
    username: str = "",
    password: str = "",
) -> dict[str, ProbeResult]:
    if not sources:
        return {}
    results: dict[str, ProbeResult] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(PROBE_WORKERS, len(sources)))) as executor:
        pending = {
            executor.submit(probe_stream, source["stream_key"], username, password): source["stream_uuid"]
            for source in sources
        }
        for future in as_completed(pending):
            stream_uuid = pending[future]
            try:
                results[stream_uuid] = future.result()
            except Exception:
                results[stream_uuid] = ProbeResult("error", 0)
    return results


def _probe_source_entries(sources: list[dict[str, str]]) -> dict[str, ProbeResult]:
    if not sources:
        return {}
    results: dict[str, ProbeResult] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(PROBE_WORKERS, len(sources)))) as executor:
        pending = {
            executor.submit(
                probe_source,
                source["uri"],
                source["username"],
                source["password"],
            ): source["stream_uuid"]
            for source in sources
        }
        for future in as_completed(pending):
            stream_uuid = pending[future]
            try:
                results[stream_uuid] = future.result()
            except Exception:
                results[stream_uuid] = ProbeResult("error", 0)
    return results


def probe_upstreams(repository: Any) -> dict[str, ProbeResult]:
    sources = repository.managed_stream_sources(include_auth_failed=False)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for source in sources:
        grouped[source["camera_uuid"]].append(source)
    first_sources = [camera_sources[0] for camera_sources in grouped.values()]
    first_results = _probe_source_entries(first_sources)
    results: dict[str, ProbeResult] = {}
    remaining: list[dict[str, str]] = []
    for camera_uuid, camera_sources in grouped.items():
        first = camera_sources[0]
        result = first_results[first["stream_uuid"]]
        if result.status == "auth_failed":
            repository.record_camera_auth_failure(camera_uuid, result)
            continue
        results[first["stream_uuid"]] = result
        remaining.extend(camera_sources[1:])
    if remaining:
        results.update(_probe_source_entries(remaining))
    repository.record_probe_results(results)
    return {**first_results, **results}


def reconcile_and_probe(repository: Any) -> dict[str, ProbeResult]:
    sources = repository.managed_stream_sources()
    revision_id, revision_status = repository.record_desired_media_revision(sources)
    try:
        reconcile_streams(sources)
        reconcile_preloads([])
        desired_keys = {source["stream_key"] for source in sources}
        if not desired_keys.issubset(runtime_stream_keys()):
            raise RuntimeError("go2rtc did not retain the desired stream set")
    except Exception:
        if revision_status == "desired":
            repository.complete_media_revision(revision_id, "failed", "runtime_apply_failed")
        raise
    if revision_status == "desired":
        repository.complete_media_revision(revision_id, "applied")
    results = probe_streams(sources, "camadmiral", repository.rtsp_access_password())
    repository.record_probe_results(results)
    return results
