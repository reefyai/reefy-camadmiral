from __future__ import annotations

import base64
from collections import deque
import hashlib
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml

try:
    from .frame_fingerprint import (
        FRAME_HEIGHT,
        FRAME_SIZE,
        FRAME_WIDTH,
        fingerprint_distance,
        frame_fingerprint,
        mean_fingerprint,
    )
except ImportError:
    from frame_fingerprint import (
        FRAME_HEIGHT,
        FRAME_SIZE,
        FRAME_WIDTH,
        fingerprint_distance,
        frame_fingerprint,
        mean_fingerprint,
    )


BASE_URL = "http://camadmiral:18080"
API_TOKEN = "synthetic-e2e-api-token"
FRIGATE_API_URL = "http://172.30.0.30:5000"
STATE_PATH = Path("/state/baseline.json")
IDENTITY_STATE_PATH = Path("/state/identity-recovery.json")
IDENTITY_FRAME_TELEMETRY_PATH = Path("/state/identity-consumer-frames.jsonl")
IDENTITY_CONTROL_CONFIG_PATH = Path("/state/identity-control.json")
IDENTITY_CONTROL_STATUS_PATH = Path("/state/identity-control-status.json")
DIRECT_RTSP_STATE_PATH = Path("/state/direct-rtsp.json")
OPEN_NAME = "Synthetic open camera"
AUTH_NAME = "Synthetic authenticated camera"
ONVIF_NAME = "Synthetic ONVIF camera"
ONVIF_ENDPOINT = "urn:uuid:synthetic-onvif-camera"
ONVIF_REPLACEMENT_ENDPOINT = "urn:uuid:synthetic-onvif-replacement"
ONVIF_ORIGINAL_IP = "172.30.0.13"
ONVIF_MOVED_IP = "172.30.0.15"
ONVIF_REPLACEMENT_IP = "172.30.0.16"
ONVIF_ORIGINAL_MAC = "02:00:00:00:00:13"
ONVIF_MOVED_MAC = "02:00:00:00:00:15"
ONVIF_REPLACEMENT_MAC = "02:00:00:00:00:16"
ONVIF_MOVED_MAIN_SIZE = (960, 540)
ONVIF_MOVED_SUB_SIZE = (480, 270)
OPEN_SECOND_MOVED_IP = "172.30.0.17"
DIRECT_ENTRANCE_NAME = "Synthetic bridge entrance"
DIRECT_LOADING_NAME = "Synthetic bridge loading"
ADMIN_PASSWORD = os.environ.get("CAMADMIRAL_E2E_ADMIN_PASSWORD", "")


class ScenarioFailure(RuntimeError):
    pass


def _read_exact(stream, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def _collect_decoded_frames(
    url: str,
    barrier: threading.Barrier,
    stop_at: float,
    arrivals: list[tuple[bytes, int]],
    errors: list[str],
) -> None:
    process = None
    try:
        barrier.wait(timeout=5)
        process = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "nobuffer",
                "-flags",
                "low_delay",
                "-avioflags",
                "direct",
                "-rtsp_transport",
                "tcp",
                "-i",
                url,
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                "scale=64:36,format=gray",
                "-fps_mode",
                "passthrough",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            raise RuntimeError("FFmpeg did not expose decoded video")
        frame_size = 64 * 36
        while time.monotonic() < stop_at:
            frame = _read_exact(process.stdout, frame_size)
            if len(frame) != frame_size:
                break
            arrivals.append((hashlib.sha256(frame).digest(), time.monotonic_ns()))
    except Exception as exc:
        errors.append(str(exc))
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return ordered[index]


def _matched_relay_delays(
    direct: list[tuple[bytes, int]],
    relayed: list[tuple[bytes, int]],
) -> list[float]:
    relay_times: dict[bytes, deque[int]] = {}
    for digest, observed_at in relayed:
        relay_times.setdefault(digest, deque()).append(observed_at)
    matched: list[float] = []
    for digest, direct_at in direct:
        observations = relay_times.get(digest)
        if observations:
            matched.append((observations.popleft() - direct_at) / 1_000_000)
    return matched


def relay_latency() -> None:
    direct: list[tuple[bytes, int]] = []
    relayed: list[tuple[bytes, int]] = []
    errors: list[str] = []
    barrier = threading.Barrier(3)
    stop_at = time.monotonic() + 12
    workers = [
        threading.Thread(
            target=_collect_decoded_frames,
            args=("rtsp://camera-open:8554/main", barrier, stop_at, direct, errors),
        ),
        threading.Thread(
            target=_collect_decoded_frames,
            args=("rtsp://latency-relay:8554/relayed", barrier, stop_at, relayed, errors),
        ),
    ]
    for worker in workers:
        worker.start()
    barrier.wait(timeout=5)
    for worker in workers:
        worker.join(timeout=20)
    if any(worker.is_alive() for worker in workers):
        raise ScenarioFailure("Timed out collecting latency benchmark frames")
    if errors:
        raise ScenarioFailure(f"Unable to collect latency benchmark frames: {errors[0]}")

    matched = _matched_relay_delays(direct, relayed)
    warmup = min(15, len(matched) // 4)
    steady = matched[warmup:]
    if len(steady) < 30:
        raise ScenarioFailure(
            "Too few identical decoded frames crossed both paths "
            f"(direct={len(direct)}, relayed={len(relayed)}, matched={len(matched)})"
        )

    median_ms = statistics.median(steady)
    p95_ms = _percentile(steady, 0.95)
    minimum_ms = min(steady)
    maximum_ms = max(steady)
    print(
        "relay-latency: "
        f"matched={len(steady)} median={median_ms:.2f}ms p95={p95_ms:.2f}ms "
        f"min={minimum_ms:.2f}ms max={maximum_ms:.2f}ms"
    )


def frigate_saved_config() -> dict[str, object]:
    with urllib.request.urlopen("http://camadmiral:5000/api/config/raw", timeout=8) as response:
        raw = json.load(response)
    parsed = yaml.safe_load(raw) if isinstance(raw, str) else None
    if not isinstance(parsed, dict):
        raise ScenarioFailure("Frigate returned an invalid saved configuration")
    return parsed


def request(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60,
) -> tuple[int, bytes, dict[str, str]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = dict(headers or {})
    if not path.startswith("/api/v1/") and path != "/healthz":
        credentials = base64.b64encode(f"admin:{ADMIN_PASSWORD}".encode()).decode()
        request_headers.setdefault("Authorization", f"Basic {credentials}")
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    web_request = urllib.request.Request(
        BASE_URL + path,
        data=body,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(web_request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def request_json(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    expected: int = 200,
    timeout: float = 60,
) -> dict[str, object]:
    status, body, _response_headers = request(
        path,
        method=method,
        payload=payload,
        headers=headers,
        timeout=timeout,
    )
    try:
        decoded = json.loads(body)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ScenarioFailure(f"{path} returned invalid JSON with HTTP {status}") from exc
    if status != expected:
        message = decoded.get("message") or decoded.get("detail") or decoded.get("status")
        raise ScenarioFailure(f"{path} returned HTTP {status}: {message}")
    return decoded


def wait_for(description: str, callback, *, timeout: float = 90, interval: float = 1):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = callback()
            if value:
                return value
        except Exception as exc:
            last_error = exc
        time.sleep(interval)
    detail = f": {last_error}" if last_error else ""
    raise ScenarioFailure(f"Timed out waiting for {description}{detail}")


def wait_for_health() -> None:
    def healthy() -> bool:
        status, body, _headers = request("/healthz", timeout=5)
        return status == 200 and json.loads(body).get("status") == "healthy"

    wait_for("CamAdmiral health", healthy)


def assert_version_surface() -> None:
    health = request_json("/healthz")
    version = str(health.get("version", ""))
    if not re.fullmatch(r"v\d{4}\.\d{2}\.\d{2}-\d{2}", version):
        raise ScenarioFailure(f"CamAdmiral reported an invalid Reefy app version: {version!r}")
    status, body, _headers = request("/")
    if status != 200 or b'data-testid="app-version"' not in body or b'fetch("/healthz"' not in body:
        raise ScenarioFailure("The app version is not wired to the UI footer")


def discovery() -> dict[str, object]:
    return request_json("/internal/discovery")


def discovery_device(candidate_uuid: str) -> dict[str, object] | None:
    return next(
        (
            device
            for device in discovery().get("devices", [])
            if device.get("candidate_uuid") == candidate_uuid
        ),
        None,
    )


def consumer_directory(*, token: str = API_TOKEN, expected: int = 200) -> dict[str, object]:
    return request_json(
        "/api/v1/cameras",
        headers={"Authorization": f"Bearer {token}"},
        expected=expected,
    )


def camera_by_name(directory: dict[str, object], name: str) -> dict[str, object]:
    camera = next((camera for camera in directory.get("cameras", []) if camera.get("name") == name), None)
    if camera is None:
        raise ScenarioFailure(f"Camera not found in consumer directory: {name}")
    return camera


def directory_signature(directory: dict[str, object]) -> dict[str, object]:
    signature: dict[str, object] = {}
    for camera in directory.get("cameras", []):
        signature[str(camera["id"])] = {
            "name": camera["name"],
            "streams": sorted(
                [
                    {
                        "id": stream["id"],
                        "roles": sorted(stream["roles"]),
                        "url": stream["downstream"]["url"],
                        "authentication": stream["downstream"]["authentication"],
                    }
                    for stream in camera.get("streams", [])
                ],
                key=lambda stream: stream["id"],
            ),
        }
    return signature


def availability(camera_uuid: str, window: str = "24h") -> dict[str, object]:
    result = request_json(
        f"/internal/cameras/{urllib.parse.quote(camera_uuid)}/availability?window={window}"
    )
    expected_buckets = 48 if window == "24h" else 56
    buckets = result.get("buckets", [])
    if result.get("window") != window or len(buckets) != expected_buckets:
        raise ScenarioFailure("Camera availability returned an invalid bounded timeline")
    valid_states = {"healthy", "degraded", "offline", "auth_failed", "unknown", "disabled"}
    if any(bucket.get("state") not in valid_states for bucket in buckets):
        raise ScenarioFailure("Camera availability returned an unknown health state")
    for bucket in buckets:
        segments = bucket.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ScenarioFailure("Camera availability omitted bucket segments")
        if any(segment.get("state") not in valid_states for segment in segments):
            raise ScenarioFailure("Camera availability returned an unknown segment state")
        if bucket.get("state") != segments[-1].get("state"):
            raise ScenarioFailure("Camera availability bucket does not end in its latest state")
        bucket_seconds = (
            datetime.fromisoformat(str(bucket["end"]))
            - datetime.fromisoformat(str(bucket["start"]))
        ).total_seconds()
        segment_seconds = sum(float(segment.get("seconds", 0)) for segment in segments)
        if abs(bucket_seconds - segment_seconds) > 0.01:
            raise ScenarioFailure("Camera availability segments do not fill their bucket")
    return result


def incidents(status: str = "all") -> dict[str, object]:
    return request_json(f"/internal/incidents?status={status}&limit=100")


def wait_for_camera_sources(
    description: str,
    expected: dict[str, tuple[int, int]],
) -> None:
    def ready() -> bool:
        for url, dimensions in expected.items():
            completed = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-rtsp_transport",
                    "tcp",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "json",
                    url,
                ],
                check=False,
                capture_output=True,
                timeout=15,
            )
            if completed.returncode != 0:
                return False
            streams = json.loads(completed.stdout).get("streams", [])
            if not streams or (streams[0].get("width"), streams[0].get("height")) != dimensions:
                return False
        return True

    wait_for(description, ready, timeout=120, interval=1)


def wait_for_open_camera_sources(host: str = "camera-open") -> None:
    wait_for_camera_sources(
        "open synthetic camera source readiness",
        {
            f"rtsp://{host}:8554/main": (1280, 720),
            f"rtsp://{host}:8554/sub": (640, 360),
        },
    )


def wait_for_baseline_camera_sources() -> None:
    wait_for_open_camera_sources()
    wait_for_camera_sources(
        "authenticated synthetic camera source readiness",
        {
            "rtsp://operator:synthetic-camera-secret@camera-auth:8554/live": (640, 360),
        },
    )
    wait_for_camera_sources(
        "ONVIF synthetic camera source readiness",
        {
            "rtsp://camera-onvif:8554/main": (1280, 720),
            "rtsp://camera-onvif:8554/sub": (640, 360),
        },
    )


def authenticated_rtsp_url(stream: dict[str, object]) -> str:
    parsed = urllib.parse.urlsplit(stream["downstream"]["url"])
    authentication = stream["downstream"]["authentication"]
    user = urllib.parse.quote(authentication["username"], safe="")
    password = urllib.parse.quote(authentication["password"], safe="")
    host = f"[{parsed.hostname}]" if ":" in str(parsed.hostname) else parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit(parsed._replace(netloc=f"{user}:{password}@{host}"))


def probe_stream(stream: dict[str, object]) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-rtsp_transport",
            "tcp",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,avg_frame_rate",
            "-of",
            "json",
            authenticated_rtsp_url(stream),
        ],
        check=False,
        capture_output=True,
        timeout=20,
    )
    if completed.returncode != 0:
        raise ScenarioFailure("A stable downstream RTSP stream is unavailable")
    payload = json.loads(completed.stdout)
    video = next((item for item in payload.get("streams", []) if item.get("codec_type") == "video"), None)
    if video is None:
        raise ScenarioFailure("A stable downstream RTSP stream has no video track")
    return video


def assert_snapshot(camera_uuid: str) -> None:
    def valid_snapshot() -> bool:
        status, body, headers = request(
            f"/internal/cameras/{camera_uuid}/snapshot.jpg", timeout=20
        )
        content_type = next(
            (value for name, value in headers.items() if name.lower() == "content-type"),
            "",
        )
        if status != 200:
            raise ScenarioFailure(f"Snapshot returned HTTP {status}")
        if not content_type.startswith("image/jpeg"):
            raise ScenarioFailure(f"Snapshot returned {content_type or 'no content type'}")
        if not body.startswith(b"\xff\xd8\xff") or not body.endswith(b"\xff\xd9"):
            raise ScenarioFailure("Snapshot returned invalid JPEG data")
        return True

    wait_for("valid camera snapshot", valid_snapshot, timeout=60, interval=1)


def assert_periodic_thumbnail(camera_uuid: str) -> None:
    def valid_thumbnail() -> bool:
        status, body, headers = request(
            f"/internal/cameras/{camera_uuid}/thumbnail.jpg", timeout=10
        )
        if status == 404:
            return False
        captured_at = next(
            (
                value for name, value in headers.items()
                if name.lower() == "x-camadmiral-captured-at"
            ),
            "",
        )
        if status != 200 or not captured_at:
            raise ScenarioFailure("Periodic camera thumbnail is unavailable")
        if not body.startswith(b"\xff\xd8\xff") or not body.endswith(b"\xff\xd9"):
            raise ScenarioFailure("Periodic camera thumbnail is not a valid JPEG")
        return True

    wait_for("periodic cached camera thumbnail", valid_thumbnail, timeout=60, interval=1)


def assert_all_media(directory: dict[str, object]) -> None:
    for camera in directory.get("cameras", []):
        for stream in camera.get("streams", []):
            wait_for(
                "decoded stable downstream media",
                lambda stream=stream: probe_stream(stream),
                timeout=120,
                interval=1,
            )
        assert_snapshot(str(camera["id"]))


def wait_for_online(names: set[str]) -> dict[str, object]:
    def ready() -> dict[str, object] | None:
        directory = consumer_directory()
        cameras = {camera.get("name"): camera for camera in directory.get("cameras", [])}
        if not names.issubset(cameras):
            return None
        if any(cameras[name].get("state") != "online" for name in names):
            return None
        if any(not cameras[name].get("streams") for name in names):
            return None
        return directory

    return wait_for("healthy consumer streams", ready, timeout=120)


def assert_stable(state: dict[str, object]) -> dict[str, object]:
    def stable_identities() -> dict[str, object] | None:
        directory = consumer_directory()
        cameras = {camera.get("name"): camera for camera in directory.get("cameras", [])}
        if not {OPEN_NAME, AUTH_NAME}.issubset(cameras):
            return None
        if any(not cameras[name].get("streams") for name in {OPEN_NAME, AUTH_NAME}):
            return None
        if directory_signature(directory) != state["signature"]:
            raise ScenarioFailure("Stable camera, stream, or downstream identity changed")
        return directory

    directory = wait_for("stable downstream identities", stable_identities, timeout=120)
    assert_all_media(directory)
    return directory


def adopt_rtsp(
    candidate_uuid: str,
    display_name: str,
    username: str,
    password: str,
    sources: list[dict[str, str]],
    *,
    expected: int = 200,
) -> dict[str, object]:
    return request_json(
        f"/internal/discovery/{candidate_uuid}/adopt-rtsp",
        method="POST",
        headers={"X-CamAdmiral-Action": "adopt-rtsp"},
        payload={
            "display_name": display_name,
            "username": username,
            "password": password,
            "sources": sources,
        },
        expected=expected,
        timeout=120,
    )


def assert_shared_upstream(stream: dict[str, object]) -> None:
    command = [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        authenticated_rtsp_url(stream),
        "-t",
        "30",
        "-f",
        "null",
        "-",
    ]
    clients = [
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(2)
    ]
    try:
        def one_upstream() -> bool:
            with urllib.request.urlopen("http://camera-open:1984/api/streams", timeout=2) as response:
                camera_streams = json.load(response)
            stream_state = camera_streams.get("sub", {})
            return (
                all(client.poll() is None for client in clients)
                and len(stream_state.get("producers", [])) == 1
                and len(stream_state.get("consumers", [])) == 1
            )

        wait_for("two downstream clients sharing one camera session", one_upstream, timeout=25, interval=0.25)
    finally:
        for client in clients:
            if client.poll() is None:
                client.terminate()
        for client in clients:
            try:
                client.wait(timeout=3)
            except subprocess.TimeoutExpired:
                client.kill()
                client.wait(timeout=3)


def _direct_rtsp_cameras() -> tuple[dict[str, object], dict[str, object]]:
    directory = consumer_directory()
    return (
        camera_by_name(directory, DIRECT_ENTRANCE_NAME),
        camera_by_name(directory, DIRECT_LOADING_NAME),
    )


def _assert_direct_rtsp_media(
    entrance: dict[str, object],
    loading: dict[str, object],
) -> None:
    if entrance.get("state") != "online" or loading.get("state") != "online":
        raise ScenarioFailure("Direct RTSP cameras are not both online")
    entrance_streams = entrance.get("streams") or []
    loading_streams = loading.get("streams") or []
    if len(entrance_streams) != 1 or len(loading_streams) != 1:
        raise ScenarioFailure("Direct RTSP cameras did not retain independent streams")
    entrance_video = probe_stream(entrance_streams[0])
    loading_video = probe_stream(loading_streams[0])
    if (entrance_video.get("width"), entrance_video.get("height")) != (1280, 720):
        raise ScenarioFailure("Direct entrance stream decoded video from the wrong source")
    if (loading_video.get("width"), loading_video.get("height")) != (960, 540):
        raise ScenarioFailure("Direct loading stream decoded video from the wrong source")


def _wait_for_direct_rtsp_frigate_video(
    expected_keys: set[str],
    description: str,
) -> None:
    last_stats: dict[str, object] = {}

    def processing() -> bool:
        nonlocal last_stats
        try:
            stats = _frigate_api_json("/api/stats")
        except Exception as exc:
            last_stats = {"api_error": type(exc).__name__}
            return False
        cameras = stats.get("cameras") or {}
        last_stats = {
            key: {
                field: (cameras.get(key) or {}).get(field)
                for field in ("camera_fps", "process_fps", "capture_pid", "ffmpeg_pid")
            }
            for key in expected_keys
        }
        return all(
            float((cameras.get(key) or {}).get("camera_fps") or 0) > 0
            for key in expected_keys
        )

    try:
        wait_for(description, processing, timeout=180, interval=2)
    except ScenarioFailure as exc:
        try:
            runtime = _frigate_api_json("/api/go2rtc/streams")
        except Exception as runtime_exc:
            runtime_summary: dict[str, object] = {
                "api_error": type(runtime_exc).__name__,
            }
        else:
            runtime_summary = {
                alias: {
                    "producers": len(stream.get("producers") or []),
                    "consumers": len(stream.get("consumers") or []),
                }
                for alias, stream in runtime.items()
                if any(alias.startswith(key) for key in expected_keys)
                and isinstance(stream, dict)
            }
        raise ScenarioFailure(
            f"{exc}; stats={last_stats}; runtime={runtime_summary}"
        ) from exc


def direct_rtsp_created() -> None:
    wait_for_health()
    wait_for_camera_sources(
        "shared RTSP bridge source readiness",
        {
            "rtsp://operator:synthetic-bridge-secret@rtsp-bridge:8554/entrance": (1280, 720),
            "rtsp://operator:synthetic-bridge-secret@rtsp-bridge:8554/loading": (960, 540),
        },
    )
    entrance, loading = wait_for(
        "direct RTSP cameras created through the browser",
        lambda: _direct_rtsp_cameras(),
        timeout=120,
    )
    if entrance["id"] == loading["id"]:
        raise ScenarioFailure("Direct RTSP paths were merged into one camera")
    if entrance["streams"][0]["id"] == loading["streams"][0]["id"]:
        raise ScenarioFailure("Direct RTSP paths share a stream identity")
    _assert_direct_rtsp_media(entrance, loading)

    direct_devices = [
        device
        for device in discovery().get("devices", [])
        if device.get("camera_origin") == "direct"
    ]
    if {device.get("display_name") for device in direct_devices} != {
        DIRECT_ENTRANCE_NAME,
        DIRECT_LOADING_NAME,
    }:
        raise ScenarioFailure("Direct RTSP cameras are missing from dashboard discovery state")
    for device in direct_devices:
        adoption = device.get("adoption") or {}
        history = request_json(
            f'/internal/cameras/{urllib.parse.quote(str(adoption["camera_uuid"]), safe="")}/identity-history'
        )
        if history.get("periods"):
            raise ScenarioFailure("Direct RTSP camera created discovery identity history")

    before = directory_signature(consumer_directory())
    scan = request_json(
        "/internal/discovery/scan",
        method="POST",
        headers={"X-CamAdmiral-Action": "scan"},
        expected=202,
    )
    scan_id = scan.get("scan_id")
    wait_for(
        "network scan alongside direct RTSP cameras",
        lambda: (
            (state := discovery()).get("scan_id") == scan_id
            and state.get("status") not in {"queued", "running"}
            and state
        ),
        timeout=120,
    )
    after = directory_signature(consumer_directory())
    if before != after:
        raise ScenarioFailure("Network scan changed direct RTSP camera identities")
    entrance, loading = _direct_rtsp_cameras()
    _assert_direct_rtsp_media(entrance, loading)
    state = {
        "signature": after,
        "entrance_camera_uuid": entrance["id"],
        "loading_camera_uuid": loading["id"],
        "entrance_stream_uuid": entrance["streams"][0]["id"],
        "loading_stream_uuid": loading["streams"][0]["id"],
    }
    DIRECT_RTSP_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    DIRECT_RTSP_STATE_PATH.chmod(0o600)
    print("direct-rtsp-created: two paths on one DNS endpoint stayed independent across a scan")


def direct_rtsp_frigate() -> None:
    state = json.loads(DIRECT_RTSP_STATE_PATH.read_text(encoding="utf-8"))

    def integration_connected() -> bool:
        configured = request_json("/internal/frigate-targets").get("targets", [])
        if any(target.get("api_url") == FRIGATE_API_URL for target in configured):
            return True
        status, _body, _headers = request(
            "/internal/frigate-targets",
            method="POST",
            payload={"name": "Synthetic Frigate", "api_url": FRIGATE_API_URL},
            headers={"X-CamAdmiral-Action": "add-frigate-target"},
            timeout=10,
        )
        return status == 201

    wait_for("direct RTSP Frigate integration", integration_connected, timeout=180, interval=2)
    target = next(
        target
        for target in request_json("/internal/frigate-targets").get("targets", [])
        if target.get("api_url") == FRIGATE_API_URL
    )
    raw_target_id = str(target["target_id"])
    target_id = urllib.parse.quote(raw_target_id, safe="")
    for camera_uuid in (state["entrance_camera_uuid"], state["loading_camera_uuid"]):
        camera_id = str(camera_uuid)
        selected = request_json(
            f"/internal/frigate-targets/{target_id}/cameras/{urllib.parse.quote(camera_id, safe='')}",
            method="POST",
            headers={"X-CamAdmiral-Action": "sync-frigate-camera"},
            expected=202,
            timeout=30,
        )
        if selected.get("selected") is not True:
            raise ScenarioFailure("Direct RTSP camera was not selected for Frigate")

        def binding_completed() -> dict[str, object] | None:
            for device in discovery().get("devices", []):
                adoption = device.get("adoption") or {}
                if str(adoption.get("camera_uuid")) != camera_id:
                    continue
                for binding in adoption.get("frigate", []):
                    if str(binding.get("target_id")) != raw_target_id:
                        continue
                    if binding.get("status") in {"applied", "error"}:
                        return binding
            return None

        binding = wait_for(
            f"direct RTSP Frigate synchronization for {camera_id}",
            binding_completed,
            timeout=180,
            interval=1,
        )
        if binding.get("status") != "applied":
            raise ScenarioFailure(
                "Direct RTSP Frigate synchronization failed with "
                f"{binding.get('error_code') or 'unknown error'}"
            )

    expected_keys = {
        _frigate_camera_key(str(state["entrance_camera_uuid"])),
        _frigate_camera_key(str(state["loading_camera_uuid"])),
    }
    wait_for(
        "direct RTSP Frigate camera configuration",
        lambda: expected_keys.issubset((frigate_saved_config().get("cameras") or {}).keys()),
        timeout=180,
        interval=2,
    )
    _wait_for_direct_rtsp_frigate_video(
        expected_keys,
        "Frigate video before direct RTSP recovery tests",
    )
    print("direct-rtsp-frigate: both logical cameras received independent Frigate resources")


def direct_rtsp_after_restart() -> None:
    wait_for_health()
    state = json.loads(DIRECT_RTSP_STATE_PATH.read_text(encoding="utf-8"))
    if directory_signature(consumer_directory()) != state["signature"]:
        raise ScenarioFailure("CamAdmiral restart changed direct RTSP identities or URLs")
    entrance, loading = _direct_rtsp_cameras()
    _assert_direct_rtsp_media(entrance, loading)
    print("direct-rtsp-restart: stable camera, stream, and downstream identities survived restart")


def direct_rtsp_dns_move() -> None:
    wait_for_health()
    state = json.loads(DIRECT_RTSP_STATE_PATH.read_text(encoding="utf-8"))
    wait_for_camera_sources(
        "moved shared RTSP bridge source readiness",
        {
            "rtsp://operator:synthetic-bridge-secret@rtsp-bridge:8554/entrance": (1280, 720),
            "rtsp://operator:synthetic-bridge-secret@rtsp-bridge:8554/loading": (960, 540),
        },
    )

    def recovered() -> tuple[dict[str, object], dict[str, object]] | None:
        cameras = _direct_rtsp_cameras()
        return cameras if all(camera.get("state") == "online" for camera in cameras) else None

    entrance, loading = wait_for(
        "direct RTSP cameras after their DNS endpoint moved",
        recovered,
        timeout=120,
        interval=2,
    )
    if directory_signature(consumer_directory()) != state["signature"]:
        raise ScenarioFailure("DNS endpoint move changed direct RTSP identities or stable URLs")
    _assert_direct_rtsp_media(entrance, loading)

    print("direct-rtsp-dns-move: stable URLs resumed from the bridge's new private address")


def direct_rtsp_path_failure() -> None:
    def isolated_failure() -> bool:
        entrance, loading = _direct_rtsp_cameras()
        return entrance.get("state") in {"degraded", "offline"} and loading.get("state") == "online"

    wait_for("one direct RTSP path to fail independently", isolated_failure, timeout=90, interval=2)
    _entrance, loading = _direct_rtsp_cameras()
    probe_stream(loading["streams"][0])
    print("direct-rtsp-path-failure: unavailable source affected only its logical camera")


def direct_rtsp_path_recovery() -> None:
    state = json.loads(DIRECT_RTSP_STATE_PATH.read_text(encoding="utf-8"))
    wait_for_camera_sources(
        "restored shared RTSP bridge source readiness",
        {
            "rtsp://operator:synthetic-bridge-secret@rtsp-bridge:8554/entrance": (1280, 720),
            "rtsp://operator:synthetic-bridge-secret@rtsp-bridge:8554/loading": (960, 540),
        },
    )
    wait_for(
        "failed direct RTSP path recovery",
        lambda: all(camera.get("state") == "online" for camera in _direct_rtsp_cameras()),
        timeout=120,
        interval=2,
    )

    entrance_uuid = urllib.parse.quote(str(state["entrance_camera_uuid"]), safe="")
    request_json(
        f"/internal/cameras/{entrance_uuid}",
        method="DELETE",
        headers={"X-CamAdmiral-Action": "unadopt-camera"},
    )

    def entrance_removed() -> dict[str, object] | None:
        current = consumer_directory()
        if any(
            camera.get("name") == DIRECT_ENTRANCE_NAME
            for camera in current.get("cameras", [])
        ):
            return None
        return current

    directory = wait_for(
        "only one direct RTSP camera after unadopt",
        entrance_removed,
        timeout=120,
    )
    loading = camera_by_name(directory, DIRECT_LOADING_NAME)
    if loading["id"] != state["loading_camera_uuid"]:
        raise ScenarioFailure("Unadopting one direct RTSP camera changed its sibling identity")
    wait_for(
        "sibling direct RTSP media after unadopt",
        lambda: probe_stream(loading["streams"][0]),
        timeout=120,
    )
    saved_cameras = wait_for(
        "Frigate saved configuration after direct RTSP unadopt",
        lambda: frigate_saved_config().get("cameras") or None,
        timeout=60,
    )
    if _frigate_camera_key(str(state["entrance_camera_uuid"])) in saved_cameras:
        raise ScenarioFailure("Unadopt left the direct RTSP camera in Frigate")
    if _frigate_camera_key(str(state["loading_camera_uuid"])) not in saved_cameras:
        raise ScenarioFailure("Unadopt removed the sibling direct RTSP camera from Frigate")
    print("direct-rtsp-isolation: path failure and unadopt affected only one logical camera")


def baseline() -> None:
    wait_for_health()
    wait_for_baseline_camera_sources()
    assert_version_surface()
    wait_for(
        "seeded discovery inventory",
        lambda: discovery_device("candidate-open") and discovery_device("candidate-auth"),
    )
    status, _body, _headers = request("/api/v1/cameras")
    if status != 401:
        raise ScenarioFailure("Consumer API accepted a missing bearer token")
    consumer_directory(token="invalid-synthetic-token", expected=401)

    request_json(
        "/internal/discovery/address",
        method="POST",
        headers={"X-CamAdmiral-Action": "scan-address"},
        payload={"address": "172.30.0.13"},
        expected=202,
    )

    def onvif_candidate() -> dict[str, object] | None:
        state = discovery()
        if state.get("status") in {"queued", "running"}:
            return None
        return next(
            (
                device for device in state.get("devices", [])
                if device.get("ip") == "172.30.0.13" and device.get("onvif")
            ),
            None,
        )

    onvif = wait_for("explicit ONVIF-by-IP discovery", onvif_candidate, timeout=60)
    onvif_adoption = request_json(
        f'/internal/discovery/{onvif["candidate_uuid"]}/adopt',
        method="POST",
        headers={"X-CamAdmiral-Action": "adopt"},
        payload={"username": "", "password": "", "allow_factory_credentials": False},
        timeout=120,
    )
    request_json(
        f'/internal/cameras/{onvif_adoption["adoption"]["camera_uuid"]}/update',
        method="POST",
        headers={"X-CamAdmiral-Action": "update-camera"},
        payload={"display_name": ONVIF_NAME},
    )

    rejected = adopt_rtsp(
        "candidate-auth",
        AUTH_NAME,
        "operator",
        "wrong-synthetic-secret",
        [{"label": "Live", "url": "rtsp://172.30.0.11:8554/live"}],
        expected=401,
    )
    if rejected.get("status") != "credentials_required":
        raise ScenarioFailure("Incorrect camera credentials were not rejected clearly")

    open_adoption = adopt_rtsp(
        "candidate-open",
        OPEN_NAME,
        "",
        "",
        [
            {"label": "High", "url": "rtsp://172.30.0.10:8554/main"},
            {"label": "Low", "url": "rtsp://172.30.0.10:8554/sub"},
        ],
    )
    auth_adoption = adopt_rtsp(
        "candidate-auth",
        AUTH_NAME,
        "operator",
        "synthetic-camera-secret",
        [{"label": "Live", "url": "rtsp://172.30.0.11:8554/live"}],
    )
    profiles = {profile["token"]: profile for profile in open_adoption["profiles"]}
    roles = open_adoption["role_tokens"]
    if profiles[roles["record"]]["width"] != 1280 or profiles[roles["detect"]]["width"] != 640:
        raise ScenarioFailure("Automatic recording and detection stream selection is incorrect")
    if auth_adoption["role_tokens"]["record"] != auth_adoption["role_tokens"]["detect"]:
        raise ScenarioFailure("A one-stream camera did not bind both consumer roles")

    directory = wait_for_online({OPEN_NAME, AUTH_NAME, ONVIF_NAME})
    for camera in directory.get("cameras", []):
        assert_periodic_thumbnail(str(camera["id"]))
    assert_all_media(directory)
    open_camera = camera_by_name(directory, OPEN_NAME)
    auth_camera = camera_by_name(directory, AUTH_NAME)
    open_availability = availability(str(open_camera["id"]))
    if open_availability.get("availability_percent") is None:
        raise ScenarioFailure("Healthy camera time was not included in availability")
    detect_stream = next(stream for stream in open_camera["streams"] if "detect" in stream["roles"])
    assert_shared_upstream(detect_stream)

    request_json(
        f'/internal/cameras/{open_camera["id"]}/enabled',
        method="POST",
        headers={"X-CamAdmiral-Action": "set-camera-enabled"},
        payload={"enabled": False},
    )
    wait_for(
        "disabled camera withdrawal",
        lambda: (
            (camera := camera_by_name(consumer_directory(), OPEN_NAME)).get("state") == "disabled"
            and not camera.get("streams")
        ),
    )
    wait_for_open_camera_sources()

    def enable_camera() -> bool:
        request_json(
            f'/internal/cameras/{open_camera["id"]}/enabled',
            method="POST",
            headers={"X-CamAdmiral-Action": "set-camera-enabled"},
            payload={"enabled": True},
            timeout=120,
        )
        return True

    wait_for("saved camera stream validation", enable_camera, timeout=180, interval=2)
    directory = wait_for_online({OPEN_NAME, AUTH_NAME, ONVIF_NAME})
    assert_all_media(directory)

    state = {
        "open_camera_uuid": open_camera["id"],
        "auth_camera_uuid": auth_camera["id"],
        "signature": directory_signature(directory),
    }
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    STATE_PATH.chmod(0o600)
    settings = request_json("/internal/notification-settings")
    if settings.get("provider") != "telegram" or settings.get("bot_configured") is not False:
        raise ScenarioFailure("Telegram notification settings did not start safely unconfigured")
    request_json(
        "/internal/notification-settings",
        method="POST",
        headers={"X-CamAdmiral-Action": "update-notification-settings"},
        payload={"enabled": True},
        expected=422,
    )
    print("baseline: adoption, roles, auth rejection, lifecycle, media, cached views, fan-out, and alert contracts passed")


def multi_subnet_discovery() -> None:
    wait_for_health()
    wait_for_camera_sources(
        "secondary-subnet synthetic camera source readiness",
        {"rtsp://camera-secondary:554/live": (640, 360)},
    )

    explicit_request = request_json(
        "/internal/discovery/address",
        method="POST",
        headers={"X-CamAdmiral-Action": "scan-address"},
        payload={"address": "172.31.0.87"},
        expected=202,
    )
    explicit_scan_id = explicit_request.get("scan_id")
    if not explicit_scan_id:
        raise ScenarioFailure("Manual discovery did not return a scan identity")

    def secondary_explicitly_found() -> dict[str, object] | None:
        state = discovery()
        if (
            state.get("scan_id") != explicit_scan_id
            or state.get("status") in {"queued", "running"}
        ):
            return None
        camera = next(
            (
                device
                for device in state.get("devices", [])
                if device.get("ip") == "172.31.0.87" and device.get("rtsp")
            ),
            None,
        )
        return state if camera else None

    explicit = wait_for(
        "manual discovery on a non-default connected subnet",
        secondary_explicitly_found,
        timeout=60,
    )
    if explicit.get("network", {}).get("subnet") != "172.31.0.0/24":
        raise ScenarioFailure("Manual discovery selected the wrong connected subnet")

    full_request = request_json(
        "/internal/discovery/scan",
        method="POST",
        headers={"X-CamAdmiral-Action": "scan"},
        expected=202,
    )
    full_scan_id = full_request.get("scan_id")
    if not full_scan_id:
        raise ScenarioFailure("Full discovery did not return a scan identity")

    def full_scan_completed() -> dict[str, object] | None:
        state = discovery()
        if (
            state.get("scan_id") != full_scan_id
            or state.get("status") in {"queued", "running"}
        ):
            return None
        return state

    scanned = wait_for(
        "full discovery completion across every connected subnet",
        full_scan_completed,
        timeout=90,
    )
    if scanned.get("scanners", {}).get("rtsp") != "complete":
        raise ScenarioFailure(
            "Full multi-subnet RTSP discovery failed under the production PID limit: "
            f"{scanned.get('scanner_errors')}"
        )
    camera = next(
        (
            device
            for device in scanned.get("devices", [])
            if device.get("ip") == "172.31.0.87" and device.get("rtsp")
        ),
        None,
    )
    if camera is None:
        raise ScenarioFailure("Full multi-subnet discovery missed the RTSP-only camera")
    if scanned.get("network", {}).get("subnet") != "172.30.0.0/24":
        raise ScenarioFailure("Full discovery did not preserve the default LAN as primary")
    raw_log = "\n".join(str(line) for line in scanned.get("raw_log", []))
    if "subnet=172.30.0.0/24" not in raw_log or "subnet=172.31.0.0/24" not in raw_log:
        raise ScenarioFailure("Full discovery did not report both connected subnets")
    print("multi-subnet-discovery: manual and full RTSP discovery passed on a non-default LAN")


def partial_subnet_preservation() -> None:
    wait_for_health()
    configuration = request_json("/internal/discovery/networks")
    selected_before = [
        str(network["cidr"])
        for network in configuration.get("networks", [])
        if network.get("selected")
    ]
    scanned_subnet = "172.30.0.0/24"
    preserved_address = "172.31.0.87"
    if scanned_subnet not in selected_before or "172.31.0.0/24" not in selected_before:
        raise ScenarioFailure("Both connected test subnets must be selected before the partial scan")

    request_json(
        "/internal/discovery/networks",
        method="PUT",
        headers={"X-CamAdmiral-Action": "save-discovery-networks"},
        payload={"selected_subnets": [scanned_subnet]},
    )
    try:
        scan_request = request_json(
            "/internal/discovery/scan",
            method="POST",
            headers={"X-CamAdmiral-Action": "scan"},
            expected=202,
        )
        scan_id = scan_request.get("scan_id")
        if not scan_id:
            raise ScenarioFailure("Partial discovery did not return a scan identity")

        def partial_scan_completed() -> dict[str, object] | None:
            state = discovery()
            if (
                state.get("scan_id") != scan_id
                or state.get("status") in {"queued", "running"}
            ):
                return None
            return state

        scanned = wait_for(
            "single-subnet discovery completion",
            partial_scan_completed,
            timeout=90,
        )
    finally:
        request_json(
            "/internal/discovery/networks",
            method="PUT",
            headers={"X-CamAdmiral-Action": "save-discovery-networks"},
            payload={"selected_subnets": selected_before},
        )

    networks = scanned.get("networks", [])
    if [str(network.get("subnet")) for network in networks] != [scanned_subnet]:
        raise ScenarioFailure(f"Partial discovery scanned unexpected networks: {networks}")
    preserved = next(
        (
            device
            for device in scanned.get("devices", [])
            if device.get("ip") == preserved_address
        ),
        None,
    )
    if preserved is None:
        raise ScenarioFailure("Partial discovery removed the camera on the unscanned subnet")
    if preserved.get("status") != "online":
        raise ScenarioFailure(
            "Partial discovery marked the camera on the unscanned subnet offline"
        )
    print("partial-subnet-preservation: unscanned camera state remained online")


def large_subnet_multicast_discovery() -> None:
    wait_for_health()

    def responder_ready() -> bool:
        # The fake camera answers any UDP datagram on its WS-Discovery port.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(1)
            probe.sendto(b"ready?", ("172.29.0.87", 3702))
            try:
                payload, _sender = probe.recvfrom(65535)
            except socket.timeout:
                return False
            return b"ProbeMatch" in payload

    wait_for("oversized-subnet camera discovery responder readiness", responder_ready)

    full_request = request_json(
        "/internal/discovery/scan",
        method="POST",
        headers={"X-CamAdmiral-Action": "scan"},
        expected=202,
    )
    scan_id = full_request.get("scan_id")
    if not scan_id:
        raise ScenarioFailure("Full discovery did not return a scan identity")

    def large_cameras_found() -> dict[str, object] | None:
        state = discovery()
        if (
            state.get("scan_id") != scan_id
            or state.get("status") in {"queued", "running"}
        ):
            return None
        onvif_camera = next(
            (
                device
                for device in state.get("devices", [])
                if device.get("ip") == "172.29.0.87" and device.get("onvif")
            ),
            None,
        )
        rtsp_camera = next(
            (
                device
                for device in state.get("devices", [])
                if device.get("ip") == "172.29.0.88" and device.get("rtsp")
            ),
            None,
        )
        return state if onvif_camera and rtsp_camera else None

    scanned = wait_for(
        "multicast ONVIF and learned-neighbor RTSP discovery on an oversized subnet",
        large_cameras_found,
        timeout=90,
    )
    raw_log = "\n".join(str(line) for line in scanned.get("raw_log", []))
    if "subnet 172.29.0.0/16 exceeds the" not in raw_log:
        raise ScenarioFailure("Full discovery did not report the oversized-subnet sweep skip")
    if "learned neighbor" not in raw_log:
        raise ScenarioFailure("Oversized subnet did not use its learned neighbor candidates")
    if (
        "RTSP: subnet 172.29.0.0/16" not in raw_log
        or "learned neighbor(s) only" not in raw_log
    ):
        raise ScenarioFailure("Oversized subnet did not bound RTSP probing to learned neighbors")
    if "sent unicast probe to 172.29.0.88:3702" not in raw_log:
        raise ScenarioFailure("Oversized subnet did not reuse the learned RTSP neighbor for ONVIF")
    scanners = scanned.get("scanners", {})
    if scanners.get("onvif") != "complete" or scanners.get("rtsp") != "complete":
        raise ScenarioFailure(f"Unexpected scanner states after oversized-subnet scan: {scanners}")
    print(
        "large-subnet-multicast-discovery: multicast ONVIF and learned-neighbor RTSP "
        "discovery passed on an oversized /16 subnet without a per-address sweep"
    )


def configured_routed_subnet_discovery() -> None:
    wait_for_health()
    configuration = request_json("/internal/discovery/networks")
    detected = [
        str(network["cidr"])
        for network in configuration.get("networks", [])
        if network.get("source") == "detected"
    ]
    large_subnet = "172.29.0.0/16"
    routed_subnet = "172.29.0.80/28"
    if large_subnet not in detected:
        raise ScenarioFailure("Large connected subnet was not listed in discovery settings")
    selected = [subnet for subnet in detected if subnet != large_subnet]
    selected.append(routed_subnet)
    saved = request_json(
        "/internal/discovery/networks",
        method="PUT",
        headers={"X-CamAdmiral-Action": "save-discovery-networks"},
        payload={"selected_subnets": selected},
    )
    by_cidr = {
        str(network["cidr"]): network
        for network in saved.get("networks", [])
    }
    if by_cidr.get(large_subnet, {}).get("selected") is not False:
        raise ScenarioFailure("Removed connected subnet remained selected")
    custom = by_cidr.get(routed_subnet)
    if not custom or custom.get("source") != "custom" or custom.get("multicast") is not False:
        raise ScenarioFailure("Custom routed subnet settings were not persisted")

    full_request = request_json(
        "/internal/discovery/scan",
        method="POST",
        headers={"X-CamAdmiral-Action": "scan"},
        expected=202,
    )
    scan_id = full_request.get("scan_id")

    def routed_cameras_found() -> dict[str, object] | None:
        state = discovery()
        if state.get("scan_id") != scan_id or state.get("status") in {"queued", "running"}:
            return None
        onvif_camera = next(
            (
                device
                for device in state.get("devices", [])
                if device.get("ip") == "172.29.0.87"
                and device.get("status") == "online"
                and device.get("onvif")
            ),
            None,
        )
        rtsp_camera = next(
            (
                device
                for device in state.get("devices", [])
                if device.get("ip") == "172.29.0.88"
                and device.get("status") == "online"
                and device.get("rtsp")
            ),
            None,
        )
        return state if onvif_camera and rtsp_camera else None

    scanned = wait_for(
        "unicast ONVIF and RTSP discovery on a configured routed subnet",
        routed_cameras_found,
        timeout=90,
    )
    custom_progress = next(
        (
            network
            for network in scanned.get("networks", [])
            if network.get("subnet") == routed_subnet
        ),
        None,
    )
    if not custom_progress or custom_progress.get("status") != "complete":
        raise ScenarioFailure("Custom routed subnet did not report completed progress")
    raw_log = "\n".join(str(line) for line in scanned.get("raw_log", []))
    if f"multicast skipped for routed subnet {routed_subnet}" not in raw_log:
        raise ScenarioFailure("Routed subnet unexpectedly used ONVIF multicast")

    request_json(
        "/internal/discovery/networks",
        method="PUT",
        headers={"X-CamAdmiral-Action": "save-discovery-networks"},
        payload={"selected_subnets": detected},
    )
    print("configured-routed-subnet-discovery: saved selection and unicast scan passed")


def load_state() -> dict[str, object]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ScenarioFailure("E2E baseline state is unavailable") from exc


def accept_ui_lifecycle_state() -> None:
    state = load_state()

    def all_cameras_ready() -> dict[str, object] | None:
        directory = consumer_directory()
        cameras = directory.get("cameras", [])
        names = {camera.get("name") for camera in cameras}
        if len(cameras) != 3 or not {OPEN_NAME, AUTH_NAME}.issubset(names):
            return None
        if any(camera.get("state") != "online" for camera in cameras):
            return None
        if any(not camera.get("streams") for camera in cameras):
            return None
        return directory

    directory = wait_for("healthy cameras after UI lifecycle", all_cameras_ready)
    previous = state["signature"]
    current = directory_signature(directory)

    def camera_signature_by_name(
        signature: dict[str, object], name: str
    ) -> dict[str, object] | None:
        return next(
            (
                {"id": camera_uuid, **camera}
                for camera_uuid, camera in signature.items()
                if camera.get("name") == name
            ),
            None,
        )

    for name in (OPEN_NAME, AUTH_NAME):
        if camera_signature_by_name(previous, name) != camera_signature_by_name(
            current, name
        ):
            raise ScenarioFailure(
                f"UI lifecycle changed untouched camera identity: {name}"
            )
    previous_onvif = camera_signature_by_name(previous, ONVIF_NAME)
    recreated = [
        {"id": camera_uuid, **camera}
        for camera_uuid, camera in current.items()
        if camera.get("name") not in {OPEN_NAME, AUTH_NAME}
    ]
    if len(recreated) != 1 or recreated[0] == previous_onvif:
        raise ScenarioFailure("UI lifecycle did not recreate the ONVIF camera identity")
    state["signature"] = current
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    STATE_PATH.chmod(0o600)
    print("ui-lifecycle: recreated ONVIF identity checkpoint saved")


def runtime_recovery() -> None:
    wait_for_health()
    assert_stable(load_state())
    print("runtime-recovery: go2rtc child restart preserved all stable streams")


def runtime_drift() -> None:
    wait_for_health()
    assert_stable(load_state())
    print("runtime-drift: out-of-band stream deletion was restored with stable identities")


def camera_outage() -> None:
    wait_for_health()
    state = load_state()

    def outage_visible() -> bool:
        directory = consumer_directory()
        if directory_signature(directory) != state["signature"]:
            raise ScenarioFailure("Camera outage changed stable downstream identity")
        open_camera = camera_by_name(directory, OPEN_NAME)
        auth_camera = camera_by_name(directory, AUTH_NAME)
        timeline = availability(str(open_camera["id"]))
        latest = timeline["buckets"][-1]
        incident_state = incidents("open")
        outage_incident = next(
            (
                incident for incident in incident_state.get("incidents", [])
                if incident.get("camera_id") == open_camera.get("id")
            ),
            None,
        )
        return (
            open_camera.get("state") in {"degraded", "offline"}
            and auth_camera.get("state") == "online"
            and latest.get("state") in {"degraded", "offline"}
            and outage_incident is not None
            and outage_incident.get("kind") == "media_offline"
        )

    wait_for("camera outage health transition", outage_visible, timeout=120)
    print("camera-outage: failure was visible without withdrawing stable downstream identities")


def camera_recovery() -> None:
    wait_for_health()
    wait_for_open_camera_sources()
    state = load_state()
    assert_stable(state)

    def recovered_summary() -> bool:
        current = discovery()
        devices = current.get("devices") or []
        open_device = next(
            (
                device
                for device in devices
                if device.get("candidate_uuid") == "candidate-open"
            ),
            None,
        )
        if open_device is None:
            return False
        streams = (open_device.get("adoption") or {}).get("streams") or []
        if not streams or not all(
            stream.get("health_status") == "healthy" for stream in streams
        ):
            return False
        summary = current.get("summary") or {}
        expected_online = sum(
            device.get("connectivity_status") == "online" for device in devices
        )
        return (
            open_device.get("connectivity_status") == "online"
            and summary.get("online") == expected_online
            and summary.get("offline") == 0
        )

    wait_for(
        "recovered media and online summary",
        recovered_summary,
        timeout=120,
    )
    timeline = availability(str(state["open_camera_uuid"]))
    if timeline.get("availability_percent") is None or timeline["availability_percent"] >= 100:
        raise ScenarioFailure("Recovered camera availability did not retain outage history")
    segment_states = [
        segment["state"]
        for bucket in timeline["buckets"]
        for segment in bucket["segments"]
    ]
    recovered_after_failure = any(
        failed_state in {"degraded", "offline"} and "healthy" in segment_states[position + 1:]
        for position, failed_state in enumerate(segment_states)
    )
    if not recovered_after_failure:
        raise ScenarioFailure("Recovered camera timeline did not preserve its state transition")
    history = incidents("resolved")
    recovered = next(
        (
            incident for incident in history.get("incidents", [])
            if incident.get("camera_id") == state["open_camera_uuid"]
            and incident.get("kind") == "media_offline"
        ),
        None,
    )
    if recovered is None or recovered.get("resolution_reason") != "recovered":
        raise ScenarioFailure("Recovered camera did not retain its resolved incident")
    print("camera-recovery: media and availability recovered without user action")


def container_restart() -> None:
    wait_for_health()
    assert_stable(load_state())
    print("container-restart: persistent identities, secrets, and streams passed")


def recovered_streams_ready(adoption: dict[str, object], expected_host: str) -> bool:
    streams = adoption.get("streams") or []
    roles = adoption.get("roles") or {}
    detect_stream_uuid = roles.get("detect")
    if not streams or not detect_stream_uuid:
        return False
    if any(
        urllib.parse.urlsplit(stream.get("uri", "")).hostname != expected_host
        or stream.get("health_status") not in {"healthy", "unknown"}
        for stream in streams
    ):
        return False
    return any(
        stream.get("stream_uuid") == detect_stream_uuid
        and stream.get("health_status") == "healthy"
        for stream in streams
    )


def address_recovery() -> None:
    wait_for_open_camera_sources("camera-open-moved")
    state = load_state()
    observation: dict[str, object] = {}

    def moved() -> bool:
        device = discovery_device("candidate-open")
        adoption = device.get("adoption") if device else None
        streams = adoption.get("streams", []) if adoption else []
        observation.clear()
        observation.update(
            {
                "candidate_ip": device.get("ip") if device else None,
                "candidate_status": device.get("status") if device else None,
                "streams": [
                    {
                        "host": urllib.parse.urlsplit(stream.get("uri", "")).hostname,
                        "health": stream.get("health_status"),
                    }
                    for stream in streams
                ],
            }
        )
        return recovered_streams_ready(adoption, "172.30.0.12")

    try:
        wait_for("validated camera address promotion", moved, timeout=180)
    except ScenarioFailure as exc:
        raise ScenarioFailure(
            f"{exc}; last observation={json.dumps(observation, sort_keys=True)}"
        ) from exc
    assert_stable(state)
    print("address-recovery: upstream moved while downstream identities stayed stable")


def moved_camera_ready() -> None:
    wait_for_open_camera_sources("camera-open-moved")
    print("camera-ready: moved synthetic source is readable")


def invalid_address() -> None:
    state = load_state()
    deadline = time.monotonic() + 15
    wait_for("one recovery validation cycle", lambda: time.monotonic() >= deadline, timeout=25)
    device = discovery_device("candidate-open")
    streams = (device.get("adoption") or {}).get("streams", []) if device else []
    if not streams or any(
        urllib.parse.urlsplit(stream.get("uri", "")).hostname != "172.30.0.10"
        for stream in streams
    ):
        raise ScenarioFailure("An unvalidated endpoint replaced the last-known-good source")
    assert_stable(state)
    print("invalid-address: rejected endpoint preserved last-known-good media")


def credential_repair() -> None:
    state = load_state()
    camera_uuid = state["auth_camera_uuid"]

    def authentication_failed() -> bool:
        device = discovery_device("candidate-auth")
        adoption = device.get("adoption") if device else None
        streams = adoption.get("streams", []) if adoption else []
        return bool(streams) and any(stream.get("health_status") == "auth_failed" for stream in streams)

    wait_for("camera authentication failure", authentication_failed, timeout=120)
    request_json(
        f"/internal/cameras/{camera_uuid}/credentials",
        method="POST",
        headers={"X-CamAdmiral-Action": "update-camera-credentials"},
        payload={"username": "operator", "password": "still-wrong-synthetic-secret"},
        expected=401,
        timeout=120,
    )
    if directory_signature(consumer_directory()) != state["signature"]:
        raise ScenarioFailure("Rejected replacement credentials changed stable downstream identity")
    request_json(
        f"/internal/cameras/{camera_uuid}/credentials",
        method="POST",
        headers={"X-CamAdmiral-Action": "update-camera-credentials"},
        payload={"username": "operator", "password": "synthetic-rotated-secret"},
        timeout=120,
    )
    assert_stable(state)
    print("credential-repair: rejection preservation and validated recovery passed")


def rotated_camera_ready() -> None:
    source = "rtsp://operator:synthetic-rotated-secret@172.30.0.11:8554/live"

    def readable() -> bool:
        try:
            completed = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-rtsp_transport", "tcp",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name",
                    "-of", "json",
                    source,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            return False
        return completed.returncode == 0

    wait_for("rotated camera media readiness", readable, timeout=90, interval=2)
    print("camera-ready: rotated synthetic source is readable")


def frigate() -> None:
    state = load_state()

    def integration_connected() -> bool:
        configured = request_json("/internal/frigate-targets").get("targets", [])
        if any(target.get("api_url") == FRIGATE_API_URL for target in configured):
            return True
        status, _body, _headers = request(
            "/internal/frigate-targets",
            method="POST",
            payload={
                "name": "Synthetic Frigate",
                "api_url": FRIGATE_API_URL,
            },
            headers={"X-CamAdmiral-Action": "add-frigate-target"},
            timeout=10,
        )
        return status == 201

    wait_for("Frigate integration setup", integration_connected, timeout=180, interval=2)
    targets = request_json("/internal/frigate-targets").get("targets", [])
    target = next(
        (item for item in targets if item.get("api_url") == FRIGATE_API_URL),
        None,
    )
    if target is None:
        raise ScenarioFailure("Frigate integration disappeared before camera selection")
    target_id = urllib.parse.quote(str(target["target_id"]), safe="")

    for address_mode, expected_host in (
        ("lan", "172.30.0.20"),
        ("localhost", "localhost"),
    ):
        preview = request_json(
            f"/internal/frigate-targets/{target_id}/cameras/"
            f"{urllib.parse.quote(str(state['open_camera_uuid']), safe='')}/config?"
            + urllib.parse.urlencode({"address_mode": address_mode})
        )
        configuration = str(preview.get("configuration") or "")
        if f"@{expected_host}:18554/" not in configuration:
            raise ScenarioFailure(
                f"Frigate {address_mode} preview did not use {expected_host}"
            )

    def frigate_json(path: str) -> dict[str, object]:
        try:
            with urllib.request.urlopen(f"http://camadmiral:5000{path}", timeout=8) as response:
                return json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return {}

    def update_frigate(path: str, payload: dict[str, object]) -> dict[str, object]:
        body = json.dumps(payload).encode("utf-8")
        api_request = urllib.request.Request(
            f"http://camadmiral:5000{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(api_request, timeout=30) as response:
                return json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ScenarioFailure(f"Could not update Frigate fixture: {path}") from exc

    def sync_camera(camera_uuid: object, description: str) -> dict[str, object]:
        camera_id = str(camera_uuid)
        selected = request_json(
            f"/internal/frigate-targets/{target_id}/cameras/"
            f"{urllib.parse.quote(camera_id, safe='')}",
            method="POST",
            headers={"X-CamAdmiral-Action": "sync-frigate-camera"},
            expected=202,
            timeout=30,
        )
        if selected.get("selected") is not True or selected.get("status") != "syncing":
            raise ScenarioFailure(f"{description} did not start asynchronously: {selected}")

        def completed() -> dict[str, object] | None:
            for device in discovery().get("devices", []):
                adoption = device.get("adoption") or {}
                if str(adoption.get("camera_uuid")) != camera_id:
                    continue
                for status in adoption.get("frigate", []):
                    if str(status.get("target_id")) != target_id:
                        continue
                    if status.get("status") in {"applied", "error"}:
                        return status
            return None

        final = wait_for(description, completed, timeout=120, interval=1)
        if final.get("status") != "applied":
            raise ScenarioFailure(
                f"{description} failed with {final.get('error_code') or 'unknown error'}"
            )
        return final

    # The fixture starts with exactly one operator-owned camera. Frigate
    # 0.17.x requires one restart when the next camera is hot-added.
    first_camera_uuid = state["open_camera_uuid"]
    sync_camera(first_camera_uuid, "first per-camera Frigate synchronization")

    def settled_uptime() -> float | bool:
        uptime = float(frigate_json("/api/stats").get("service", {}).get("uptime") or 0)
        return uptime if uptime >= 5 else False

    uptime_after_required_restart = float(
        wait_for("Frigate restart settlement", settled_uptime, timeout=90, interval=1)
    )

    second_camera_uuid = state["auth_camera_uuid"]
    sync_camera(second_camera_uuid, "second per-camera Frigate synchronization")

    uptime_after_third_camera = float(
        frigate_json("/api/stats").get("service", {}).get("uptime") or 0
    )
    if uptime_after_third_camera < uptime_after_required_restart:
        raise ScenarioFailure("Frigate restarted while adding a third configured camera")

    def applied() -> tuple[dict[str, object], dict[str, object]] | None:
        config = frigate_json("/api/config")
        streams = frigate_json("/api/go2rtc/streams")
        expected = {
            "camadmiral_" + re.sub(r"[^a-zA-Z0-9_]", "_", str(state["open_camera_uuid"])),
            "camadmiral_" + re.sub(r"[^a-zA-Z0-9_]", "_", str(state["auth_camera_uuid"])),
        }
        cameras = config.get("cameras", {})
        if not expected.issubset(cameras):
            return None
        aliases = {
            alias
            for key in expected
            for alias in (f"{key}_record", f"{key}_detect")
        }
        if not aliases.issubset(streams):
            return None
        return config, streams

    config, streams = wait_for("Frigate camera injection", applied, timeout=180, interval=2)
    camera_key = "camadmiral_" + re.sub(
        r"[^a-zA-Z0-9_]", "_", str(first_camera_uuid)
    )
    if not config["cameras"][camera_key].get("live", {}).get("streams", {}):
        raise ScenarioFailure("Frigate camera has no live stream mapping")

    saved_camera = frigate_saved_config().get("cameras", {}).get(camera_key, {})
    if "fps" in saved_camera.get("detect", {}):
        raise ScenarioFailure("CamAdmiral injected a camera-level detect FPS")
    if config["cameras"][camera_key].get("detect", {}).get("fps") != 5:
        raise ScenarioFailure("Frigate camera did not inherit the global detect FPS")
    saved_streams = frigate_saved_config().get("go2rtc", {}).get("streams", {})
    managed_sources = [
        str(source)
        for alias, sources in saved_streams.items()
        if str(alias).startswith(camera_key)
        for source in (sources if isinstance(sources, list) else [sources])
    ]
    if not managed_sources or not all(
        "@172.30.0.20:18554/" in source for source in managed_sources
    ):
        raise ScenarioFailure("Frigate LAN synchronization used the wrong CamAdmiral host")

    updated = update_frigate(
        "/api/config/set",
        {
            "requires_restart": 0,
            "config_data": {"cameras": {camera_key: {"detect": {"fps": 12}}}},
        },
    )
    if updated.get("success") is not True:
        raise ScenarioFailure("Could not seed a legacy camera-level detect FPS")
    sync_camera(first_camera_uuid, "Frigate FPS inheritance synchronization")

    saved_camera = frigate_saved_config().get("cameras", {}).get(camera_key, {})
    if "fps" in saved_camera.get("detect", {}):
        raise ScenarioFailure("Camera-level detect FPS remained after synchronization")
    config = frigate_json("/api/config")
    if config["cameras"][camera_key].get("detect", {}).get("fps") != 5:
        raise ScenarioFailure("Global detect FPS was not restored after synchronization")

    last_stats: dict[str, object] = {}
    def frigate_processing() -> bool:
        nonlocal last_stats
        camera_stats = frigate_json("/api/stats").get("cameras", {}).get(camera_key, {})
        last_stats = {
            field: camera_stats.get(field)
            for field in ("camera_fps", "process_fps", "capture_pid", "ffmpeg_pid")
        } if isinstance(camera_stats, dict) else {}
        return float(camera_stats.get("camera_fps") or 0) > 0

    try:
        wait_for("Frigate camera processing", frigate_processing, timeout=90, interval=2)
    except ScenarioFailure as exc:
        runtime = frigate_json("/api/go2rtc/streams")
        runtime_summary = {
            name: {
                "producers": len(state.get("producers") or []),
                "consumers": len(state.get("consumers") or []),
            }
            for name, state in runtime.items()
            if name.startswith(camera_key) and isinstance(state, dict)
        }
        raise ScenarioFailure(
            f"{exc}; stats={last_stats}; runtime={runtime_summary}"
        ) from exc

    expected_camera_keys = [
        "camadmiral_" + re.sub(r"[^a-zA-Z0-9_]", "_", str(camera_uuid))
        for camera_uuid in (first_camera_uuid, second_camera_uuid)
    ]

    def latest_frames_are_visible() -> bool:
        for expected_key in expected_camera_keys:
            decoded = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    f"http://camadmiral:5000/api/{expected_key}/latest.webp",
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=32:18,format=gray",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "gray",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=10,
            )
            if decoded.returncode != 0 or not decoded.stdout:
                return False
            if statistics.fmean(decoded.stdout) < 5:
                return False
        return True

    wait_for(
        "visible Frigate frames after the 0.17 second-camera restart",
        latest_frames_are_visible,
        timeout=90,
        interval=2,
    )

    partial_drift_stream = "camadmiral_synthetic_partial_drift"
    partial_drift_source = "rtsp://camera-open:8554/sub"
    seeded = update_frigate(
        "/api/config/set",
        {
            "requires_restart": 0,
            "config_data": {
                "go2rtc": {"streams": {partial_drift_stream: [partial_drift_source]}}
            },
        },
    )
    if seeded.get("success") is not True:
        raise ScenarioFailure("Could not seed partial Frigate runtime drift")
    update_frigate(
        f"/api/go2rtc/streams/{partial_drift_stream}?"
        + urllib.parse.urlencode({"src": partial_drift_source}),
        {},
    )
    delete_request = urllib.request.Request(
        f"http://camadmiral:5000/api/go2rtc/streams/{partial_drift_stream}",
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(delete_request, timeout=8) as response:
            response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise ScenarioFailure("Could not create partial Frigate runtime drift") from exc
    if partial_drift_stream in frigate_json("/api/go2rtc/streams"):
        raise ScenarioFailure("Partial-drift Frigate stream remained in runtime")

    drift_preview = request_json(f"/internal/frigate-targets/{target_id}/full-sync")
    if drift_preview.get("stale_cameras") != 0 or drift_preview.get("stale_streams") != 1:
        raise ScenarioFailure(
            f"Partial-drift full sync preview returned unexpected counts: {drift_preview}"
        )
    drift_result = request_json(
        f"/internal/frigate-targets/{target_id}/full-sync",
        method="POST",
        headers={"X-CamAdmiral-Action": "full-sync-frigate-target"},
        timeout=120,
    )
    if drift_result.get("removed_cameras") != 0 or drift_result.get("removed_streams") != 1:
        raise ScenarioFailure(
            f"Partial-drift full sync returned unexpected counts: {drift_result}"
        )
    drift_paths = frigate_json("/api/config/raw_paths")
    if partial_drift_stream in drift_paths.get("go2rtc", {}).get("streams", {}):
        raise ScenarioFailure("Full sync left the partial-drift stream in Frigate config")

    stale_camera = "camadmiral_synthetic_stale"
    operator_camera = "operator_camera"
    stale_streams = {
        "camadmiral_synthetic_stale_record",
        "camadmiral_synthetic_stale_detect",
    }

    stale_sources = {
        "camadmiral_synthetic_stale_record": ["rtsp://camera-open:8554/main"],
        "camadmiral_synthetic_stale_detect": ["rtsp://camera-open:8554/sub"],
    }
    seeded = update_frigate(
        "/api/config/set",
        {
            "requires_restart": 0,
            "config_data": {"go2rtc": {"streams": stale_sources}},
        },
    )
    if seeded.get("success") is not True:
        raise ScenarioFailure("Could not seed stale Frigate streams")
    for stream_name, sources in stale_sources.items():
        update_frigate(
            f"/api/go2rtc/streams/{stream_name}?"
            + urllib.parse.urlencode({"src": sources[0]}),
            {},
        )
    seeded = update_frigate(
        "/api/config/set",
        {
            "requires_restart": 1,
            "update_topic": f"config/cameras/{stale_camera}/add",
            "config_data": {
                "cameras": {
                    stale_camera: {
                        "enabled": False,
                        "ffmpeg": {
                            "inputs": [
                                {
                                    "path": (
                                        "rtsp://127.0.0.1:8554/"
                                        "camadmiral_synthetic_stale_record"
                                    ),
                                    "input_args": "preset-rtsp-restream",
                                    "roles": ["record", "detect"],
                                }
                            ]
                        },
                        "detect": {"width": 1280, "height": 720, "fps": 5},
                        "live": {
                            "streams": {
                                "Record": "camadmiral_synthetic_stale_record",
                                "Detect": "camadmiral_synthetic_stale_detect",
                            }
                        },
                    }
                }
            },
        },
    )
    if seeded.get("success") is not True:
        raise ScenarioFailure("Could not seed stale Frigate camera")

    preview = request_json(f"/internal/frigate-targets/{target_id}/full-sync")
    if preview.get("stale_cameras") != 1 or preview.get("stale_streams") != 2:
        raise ScenarioFailure(f"Full sync preview returned unexpected counts: {preview}")
    uptime_before_full_sync = float(
        frigate_json("/api/stats").get("service", {}).get("uptime") or 0
    )
    result = request_json(
        f"/internal/frigate-targets/{target_id}/full-sync",
        method="POST",
        headers={"X-CamAdmiral-Action": "full-sync-frigate-target"},
        timeout=120,
    )
    if result.get("removed_cameras") != 1 or result.get("removed_streams") != 2:
        raise ScenarioFailure(f"Full sync returned unexpected counts: {result}")
    uptime_after_full_sync = float(
        frigate_json("/api/stats").get("service", {}).get("uptime") or 0
    )
    if uptime_after_full_sync < uptime_before_full_sync:
        raise ScenarioFailure("Frigate restarted while full sync removed stale resources")
    cleaned_config = frigate_saved_config()
    cleaned_cameras = cleaned_config.get("cameras", {})
    if stale_camera in cleaned_cameras:
        raise ScenarioFailure("Full sync left the stale CamAdmiral camera in Frigate")
    if operator_camera not in cleaned_cameras:
        raise ScenarioFailure("Full sync removed an operator-owned Frigate camera")
    cleaned_streams = cleaned_config.get("go2rtc", {}).get("streams", {})
    if stale_streams.intersection(cleaned_streams):
        raise ScenarioFailure("Full sync left stale CamAdmiral streams in Frigate")
    if "operator_stream" not in cleaned_streams:
        raise ScenarioFailure("Full sync removed an operator-owned Frigate stream")

    targets = request_json("/internal/frigate-targets").get("targets", [])
    refreshed_target = next(
        (
            item
            for item in targets
            if str(item.get("target_id")) == urllib.parse.unquote(target_id)
        ),
        None,
    )
    if refreshed_target is None:
        raise ScenarioFailure("Frigate integration disappeared after full sync")
    if bool(refreshed_target.get("restart_recommended")) != bool(
        result.get("restart_recommended")
    ):
        raise ScenarioFailure("Frigate restart-required state was not persisted")

    uptime_before_removal = float(
        frigate_json("/api/stats").get("service", {}).get("uptime") or 0
    )
    removed = request_json(
        f"/internal/frigate-targets/{target_id}/cameras/"
        f"{urllib.parse.quote(str(second_camera_uuid), safe='')}",
        method="DELETE",
        headers={"X-CamAdmiral-Action": "remove-frigate-camera"},
        timeout=30,
    )
    if removed.get("selected") is not False:
        raise ScenarioFailure(f"Per-camera Frigate removal failed: {removed}")
    if removed.get("restart_recommended") is not True:
        raise ScenarioFailure("Frigate 0.17 removal did not require a deferred restart")
    uptime_after_removal = float(
        frigate_json("/api/stats").get("service", {}).get("uptime") or 0
    )
    if uptime_after_removal < uptime_before_removal:
        raise ScenarioFailure("Frigate restarted while removing a managed camera")
    expected_camera_keys = expected_camera_keys[:1]
    wait_for(
        "remaining Frigate camera after managed camera removal",
        latest_frames_are_visible,
        timeout=30,
        interval=2,
    )

    removed_key = "camadmiral_" + re.sub(
        r"[^a-zA-Z0-9_]", "_", str(second_camera_uuid)
    )
    saved_after_removal = frigate_saved_config()
    if removed_key in saved_after_removal.get("cameras", {}):
        raise ScenarioFailure("Deferred removal remained in saved Frigate config")
    live_after_removal = frigate_json("/api/config").get("cameras", {})
    if removed_key not in live_after_removal:
        raise ScenarioFailure(
            "Frigate removal did not leave a detectable restart-required state"
        )

    operator_removed = update_frigate(
        "/api/config/set",
        {
            "requires_restart": 1,
            "config_data": {"cameras": {operator_camera: ""}},
        },
    )
    if operator_removed.get("success") is not True:
        raise ScenarioFailure("Could not remove operator fixture before final-camera test")

    final_removed = request_json(
        f"/internal/frigate-targets/{target_id}/cameras/"
        f"{urllib.parse.quote(str(first_camera_uuid), safe='')}",
        method="DELETE",
        headers={"X-CamAdmiral-Action": "remove-frigate-camera"},
        timeout=30,
    )
    if final_removed.get("selected") is not False:
        raise ScenarioFailure(f"Final Frigate camera removal failed: {final_removed}")
    saved_after_final_removal = frigate_saved_config()
    if saved_after_final_removal.get("cameras") != {}:
        raise ScenarioFailure(
            "Final Frigate camera removal did not preserve a valid empty cameras mapping"
        )

    print("frigate: final-camera removal persisted and operator restart is required")


def frigate_restart_verify() -> None:
    def frigate_ready() -> bool:
        try:
            with urllib.request.urlopen(
                "http://camadmiral:5000/api/stats", timeout=8
            ) as response:
                payload = json.load(response)
            return bool(payload.get("service"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return False

    wait_for("Frigate after operator restart", frigate_ready, timeout=120, interval=1)
    targets = request_json("/internal/frigate-targets").get("targets", [])
    target = next(
        (item for item in targets if item.get("api_url") == FRIGATE_API_URL),
        None,
    )
    if target is None:
        raise ScenarioFailure("Frigate integration missing after operator restart")
    target_id = urllib.parse.quote(str(target["target_id"]), safe="")
    checked = request_json(
        f"/internal/frigate-targets/{target_id}/test",
        method="POST",
        headers={"X-CamAdmiral-Action": "test-frigate-target"},
        timeout=30,
    )
    checked_target = checked.get("target", {})
    if checked_target.get("restart_recommended") is not False:
        raise ScenarioFailure("Frigate restart-required state did not clear after restart")
    print("frigate: injection, processing, and CamAdmiral-only full sync passed")


def frigate_unadopt() -> None:
    targets = request_json("/internal/frigate-targets").get("targets", [])
    target = next(
        (item for item in targets if item.get("api_url") == FRIGATE_API_URL),
        None,
    )
    if target is None:
        raise ScenarioFailure("Frigate integration missing for unadopt test")
    target_id = urllib.parse.quote(str(target["target_id"]), safe="")

    directory = consumer_directory()
    lifecycle_cameras = [
        camera
        for camera in directory.get("cameras", [])
        if camera.get("name") not in {OPEN_NAME, AUTH_NAME}
    ]
    if len(lifecycle_cameras) != 1:
        raise ScenarioFailure(
            f"Expected one dedicated unadopt camera, found {len(lifecycle_cameras)}"
        )
    camera = lifecycle_cameras[0]
    camera_uuid = str(camera["id"])
    candidate = next(
        (
            device
            for device in discovery().get("devices", [])
            if str((device.get("adoption") or {}).get("camera_uuid")) == camera_uuid
        ),
        None,
    )
    if candidate is None:
        raise ScenarioFailure("ONVIF candidate missing before Frigate unadopt test")
    candidate_uuid = str(candidate["candidate_uuid"])

    selected = request_json(
        f"/internal/frigate-targets/{target_id}/cameras/"
        f"{urllib.parse.quote(camera_uuid, safe='')}",
        method="POST",
        headers={"X-CamAdmiral-Action": "sync-frigate-camera"},
        expected=202,
        timeout=30,
    )
    if selected.get("selected") is not True:
        raise ScenarioFailure(f"Frigate unadopt fixture was not selected: {selected}")

    camera_key = "camadmiral_" + re.sub(r"[^a-zA-Z0-9_]", "_", camera_uuid)

    def synced() -> bool:
        saved = frigate_saved_config()
        cameras = saved.get("cameras", {})
        streams = saved.get("go2rtc", {}).get("streams", {})
        if camera_key not in cameras or not all(
            alias in streams for alias in (f"{camera_key}_record", f"{camera_key}_detect")
        ):
            return False
        current = discovery_device(candidate_uuid)
        if current is None:
            return False
        adoption = current.get("adoption") or {}
        target_status = next(
            (
                status
                for status in adoption.get("frigate", [])
                if str(status.get("target_id")) == str(target["target_id"])
            ),
            None,
        )
        return target_status is not None and target_status.get("status") == "applied"

    wait_for("completed Frigate camera sync before unadopt", synced, timeout=120, interval=1)

    unadopt_path = f"/internal/cameras/{urllib.parse.quote(camera_uuid, safe='')}"
    deadline = time.monotonic() + 30
    while True:
        status, body, _response_headers = request(
            unadopt_path,
            method="DELETE",
            headers={"X-CamAdmiral-Action": "unadopt-camera"},
            timeout=120,
        )
        try:
            removed = json.loads(body)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ScenarioFailure(
                f"{unadopt_path} returned invalid JSON with HTTP {status}"
            ) from exc
        if status == 200:
            break
        if (
            status == 409
            and removed.get("status") == "sync_busy"
            and time.monotonic() < deadline
        ):
            time.sleep(0.5)
            continue
        message = removed.get("message") or removed.get("detail") or removed.get("status")
        raise ScenarioFailure(f"{unadopt_path} returned HTTP {status}: {message}")
    if removed.get("status") != "unadopted":
        raise ScenarioFailure(f"Camera unadopt failed: {removed}")
    if removed.get("restart_recommended") is not True:
        raise ScenarioFailure("Frigate-backed unadopt did not recommend a restart")

    saved = frigate_saved_config()
    if camera_key in saved.get("cameras", {}):
        raise ScenarioFailure("Unadopt left the camera in saved Frigate configuration")
    remaining_streams = saved.get("go2rtc", {}).get("streams", {})
    if any(
        alias in remaining_streams
        for alias in (f"{camera_key}_record", f"{camera_key}_detect")
    ):
        raise ScenarioFailure("Unadopt left camera streams in saved Frigate configuration")

    def candidate_is_unadopted() -> dict[str, object] | None:
        device = discovery_device(candidate_uuid)
        if (
            device is None
            or device.get("adoption")
            or device.get("connectivity_status") != "online"
        ):
            return None
        return device

    wait_for("online unadopted ONVIF candidate", candidate_is_unadopted)
    adoption = request_json(
        f"/internal/discovery/{urllib.parse.quote(candidate_uuid, safe='')}/adopt",
        method="POST",
        headers={"X-CamAdmiral-Action": "adopt"},
        payload={"username": "", "password": "", "allow_factory_credentials": False},
        timeout=120,
    )
    replacement_uuid = str((adoption.get("adoption") or {}).get("camera_uuid") or "")
    if not replacement_uuid or replacement_uuid == camera_uuid:
        raise ScenarioFailure("Re-adoption did not create a replacement camera identity")
    request_json(
        f"/internal/cameras/{urllib.parse.quote(replacement_uuid, safe='')}/update",
        method="POST",
        headers={"X-CamAdmiral-Action": "update-camera"},
        payload={"display_name": ONVIF_NAME},
    )
    wait_for_online({OPEN_NAME, AUTH_NAME, ONVIF_NAME})
    print("frigate: full unadopt cleanup and ONVIF re-adoption passed")


AMBIGUOUS_DELETE_STREAM = "camadmiral_synthetic_ambiguous_delete_detect"
AMBIGUOUS_DELETE_SOURCE = "rtsp://camera-open:8554/sub"


def frigate_ambiguous_delete_setup() -> None:
    body = json.dumps(
        {
            "requires_restart": 0,
            "config_data": {
                "go2rtc": {
                    "streams": {AMBIGUOUS_DELETE_STREAM: [AMBIGUOUS_DELETE_SOURCE]}
                }
            },
        }
    ).encode("utf-8")
    config_request = urllib.request.Request(
        "http://camadmiral:5000/api/config/set",
        data=body,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(config_request, timeout=30) as response:
        configured = json.load(response)
    if configured.get("success") is not True:
        raise ScenarioFailure("Could not save ambiguous-delete Frigate stream")

    runtime_path = (
        f"http://camadmiral:5000/api/go2rtc/streams/{AMBIGUOUS_DELETE_STREAM}?"
        + urllib.parse.urlencode({"src": AMBIGUOUS_DELETE_SOURCE})
    )
    runtime_request = urllib.request.Request(
        runtime_path,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(runtime_request, timeout=30) as response:
        runtime_result = json.load(response)
    if runtime_result.get("success") is not True:
        raise ScenarioFailure("Could not create ambiguous-delete live stream")
    print("frigate: ambiguous-delete fixture ready")


def frigate_ambiguous_delete_verify() -> None:
    targets = request_json("/internal/frigate-targets").get("targets", [])
    target = next(
        (item for item in targets if item.get("api_url") == FRIGATE_API_URL),
        None,
    )
    if target is None:
        raise ScenarioFailure("Frigate integration missing for ambiguous-delete test")
    target_id = urllib.parse.quote(str(target["target_id"]), safe="")
    preview = request_json(f"/internal/frigate-targets/{target_id}/full-sync")
    stale_stream_count = int(preview.get("stale_streams") or 0)
    if stale_stream_count < 1:
        raise ScenarioFailure(f"Ambiguous-delete preview was unexpected: {preview}")
    result = request_json(
        f"/internal/frigate-targets/{target_id}/full-sync",
        method="POST",
        headers={"X-CamAdmiral-Action": "full-sync-frigate-target"},
        timeout=120,
    )
    if result.get("removed_streams") != stale_stream_count:
        raise ScenarioFailure(f"Ambiguous-delete full sync failed: {result}")

    with urllib.request.urlopen(
        "http://camadmiral:5000/api/go2rtc/streams", timeout=8
    ) as response:
        runtime = json.load(response)
    if AMBIGUOUS_DELETE_STREAM in runtime:
        raise ScenarioFailure("Ambiguous-delete stream remained in live go2rtc state")
    saved_streams = frigate_saved_config().get("go2rtc", {}).get("streams", {})
    if AMBIGUOUS_DELETE_STREAM in saved_streams:
        raise ScenarioFailure("Ambiguous-delete stream remained in saved Frigate config")
    print("frigate: ambiguous partial-success deletion recovered")


def _load_identity_state() -> dict[str, object]:
    try:
        return json.loads(IDENTITY_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ScenarioFailure("Identity recovery E2E state is unavailable") from exc


def _device_by_onvif_endpoint(endpoint: str) -> dict[str, object] | None:
    expected = endpoint.lower()
    return next(
        (
            device
            for device in discovery().get("devices", [])
            if str((device.get("onvif") or {}).get("endpoint_reference") or "").lower()
            == expected
        ),
        None,
    )


def _camera_by_id(directory: dict[str, object], camera_uuid: str) -> dict[str, object] | None:
    return next(
        (
            camera
            for camera in directory.get("cameras", [])
            if str(camera.get("id")) == camera_uuid
        ),
        None,
    )


def _frigate_camera_key(camera_uuid: str) -> str:
    return "camadmiral_" + re.sub(r"[^a-zA-Z0-9_]", "_", camera_uuid)


def _frigate_api_json(path: str) -> dict[str, object]:
    try:
        with urllib.request.urlopen(f"http://camadmiral:5000{path}", timeout=8) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _frigate_latest_fingerprint(camera_key: str) -> list[float] | None:
    try:
        decoded = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                f"http://camadmiral:5000/api/{camera_key}/latest.webp",
                "-frames:v",
                "1",
                "-vf",
                f"scale={FRAME_WIDTH}:{FRAME_HEIGHT},format=rgb24",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            capture_output=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None
    if decoded.returncode != 0 or len(decoded.stdout) < FRAME_SIZE:
        return None
    return frame_fingerprint(decoded.stdout[:FRAME_SIZE])


def _stream_fingerprint(url: str) -> list[float]:
    try:
        decoded = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-rtsp_transport",
                "tcp",
                "-i",
                url,
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                f"scale={FRAME_WIDTH}:{FRAME_HEIGHT},format=rgb24",
                "-frames:v",
                "5",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise ScenarioFailure(f"Timed out fingerprinting {url}") from exc
    if decoded.returncode != 0 or len(decoded.stdout) < FRAME_SIZE * 3:
        raise ScenarioFailure(f"Could not decode enough frames to fingerprint {url}")
    frames = [
        decoded.stdout[offset : offset + FRAME_SIZE]
        for offset in range(0, len(decoded.stdout) - FRAME_SIZE + 1, FRAME_SIZE)
    ]
    return mean_fingerprint([frame_fingerprint(frame) for frame in frames])


def _consumer_status(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _identity_consumer_status() -> dict[str, object]:
    return _consumer_status(Path("/state/identity-consumer-status.json"))


def _identity_consumer_frames() -> list[dict[str, object]]:
    try:
        lines = IDENTITY_FRAME_TELEMETRY_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    frames: list[dict[str, object]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            if index == len(lines) - 1:
                continue
            raise ScenarioFailure("Identity consumer telemetry is malformed") from exc
        if not isinstance(record, dict):
            raise ScenarioFailure("Identity consumer telemetry record is malformed")
        frames.append(record)
    return frames


def _identity_moved_frame_transition(
    *,
    outage_started_at: float,
    reconnect_not_before: float,
    original_session_id: str,
    moved_session_id: str,
    original_fingerprint: list[float],
    moved_fingerprint: list[float],
    source_separation: float,
) -> dict[str, float | int] | None:
    records = _identity_consumer_frames()
    if not records:
        return None

    def classified(record: dict[str, object], expected: list[float], other: list[float]) -> bool:
        fingerprint = record.get("fingerprint")
        if not isinstance(fingerprint, list):
            return False
        expected_distance = fingerprint_distance(fingerprint, expected)
        other_distance = fingerprint_distance(fingerprint, other)
        return (
            expected_distance <= max(8, source_separation * 0.35)
            and expected_distance < other_distance
        )

    frames = [record for record in records if record.get("event") == "frame"]
    moved_run = 3
    for index in range(len(frames) - moved_run + 1):
        first = frames[index]
        try:
            first_decoded_at = float(first["decoded_at"])
            first_frame_index = int(first["frame_index"])
        except (KeyError, TypeError, ValueError):
            continue
        if first_decoded_at < outage_started_at:
            continue
        window = frames[index : index + moved_run]
        if any(
            str(record.get("session_id") or "") != moved_session_id
            for record in window
        ):
            continue
        try:
            frame_indexes = [int(record["frame_index"]) for record in window]
        except (KeyError, TypeError, ValueError):
            continue
        if frame_indexes != list(range(first_frame_index, first_frame_index + moved_run)):
            continue
        if not all(
            classified(record, moved_fingerprint, original_fingerprint)
            for record in window
        ):
            continue

        preceding = [
            record
            for record in frames[:index]
            if str(record.get("session_id") or "") == original_session_id
            and classified(record, original_fingerprint, moved_fingerprint)
        ]
        if not preceding:
            raise ScenarioFailure(
                "Identity consumer telemetry has moved frames without original frames"
            )
        try:
            last_original_at = max(float(record["decoded_at"]) for record in preceding)
        except (KeyError, TypeError, ValueError) as exc:
            raise ScenarioFailure(
                "Identity consumer telemetry has an invalid decode timestamp"
            ) from exc

        transition_frames = [
            record
            for record in frames[: index + 1]
            if str(record.get("session_id") or "")
            in {original_session_id, moved_session_id}
        ]
        maximum_gap = 0.0
        for previous, current in zip(transition_frames, transition_frames[1:]):
            try:
                current_at = float(current["decoded_at"])
                previous_at = float(previous["decoded_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if current_at >= outage_started_at:
                maximum_gap = max(maximum_gap, current_at - previous_at)
        original_exit = next(
            (
                record
                for record in records
                if record.get("event") == "session_exited"
                and str(record.get("session_id") or "") == original_session_id
            ),
            None,
        )
        moved_start = next(
            (
                record
                for record in records
                if record.get("event") == "session_started"
                and str(record.get("session_id") or "") == moved_session_id
            ),
            None,
        )
        if original_exit is None or moved_start is None:
            return None
        try:
            original_exited_at = float(original_exit["exited_at"])
            moved_started_at = float(moved_start["started_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if (
            original_exited_at < outage_started_at
            or moved_started_at < reconnect_not_before
            or moved_started_at < original_exited_at
        ):
            return None
        return {
            "first_moved_frame_at": first_decoded_at,
            "first_moved_frame_index": first_frame_index,
            "last_original_frame_at": last_original_at,
            "original_session_exited_at": original_exited_at,
            "moved_session_started_at": moved_started_at,
            "maximum_frame_gap": maximum_gap,
        }
    return None


def _assert_identity_period(
    period: dict[str, object],
    *,
    ip: str,
    mac: str,
    endpoint: str,
    current: bool,
) -> None:
    expected = {
        "ip": ip,
        "mac": mac,
        "onvif_identity": endpoint,
        "current": current,
    }
    observed = {field: period.get(field) for field in expected}
    if observed != expected:
        raise ScenarioFailure(
            f"Camera identity period was unexpected: expected={expected}, observed={observed}"
        )


def identity_recovery_setup() -> None:
    wait_for_health()
    wait_for_camera_sources(
        "original ONVIF identity source readiness",
        {
            f"rtsp://{ONVIF_ORIGINAL_IP}:8554/main": (1280, 720),
            f"rtsp://{ONVIF_ORIGINAL_IP}:8554/sub": (640, 360),
        },
    )

    def adopted_onvif() -> dict[str, object] | None:
        device = _device_by_onvif_endpoint(ONVIF_ENDPOINT)
        adoption = device.get("adoption") if device else None
        if (
            device is None
            or device.get("status") != "online"
            or str(device.get("ip")) != ONVIF_ORIGINAL_IP
            or str(device.get("mac") or "").lower() != ONVIF_ORIGINAL_MAC
            or not adoption
            or not adoption.get("streams")
        ):
            return None
        return device

    device = wait_for("adopted ONVIF identity recovery fixture", adopted_onvif)
    candidate_uuid = str(device["candidate_uuid"])
    adoption = device["adoption"]
    camera_uuid = str(adoption["camera_uuid"])

    def target_ready() -> dict[str, object] | None:
        return next(
            (
                target
                for target in request_json("/internal/frigate-targets").get("targets", [])
                if target.get("api_url") == FRIGATE_API_URL
            ),
            None,
        )

    target = wait_for("existing Frigate identity recovery target", target_ready, timeout=120)
    target_id = str(target["target_id"])
    encoded_target_id = urllib.parse.quote(target_id, safe="")
    selected = request_json(
        f"/internal/frigate-targets/{encoded_target_id}/cameras/"
        f"{urllib.parse.quote(camera_uuid, safe='')}",
        method="POST",
        headers={"X-CamAdmiral-Action": "sync-frigate-camera"},
        expected=202,
        timeout=30,
    )
    if selected.get("selected") is not True or selected.get("status") != "syncing":
        raise ScenarioFailure(f"Identity recovery Frigate sync did not start: {selected}")

    def binding_applied() -> dict[str, object] | None:
        current = discovery_device(candidate_uuid)
        current_adoption = current.get("adoption") if current else None
        for status in (current_adoption or {}).get("frigate", []):
            if str(status.get("target_id")) != target_id:
                continue
            if status.get("status") == "error":
                raise ScenarioFailure(
                    "Identity recovery Frigate sync failed with "
                    f"{status.get('error_code') or 'unknown error'}"
                )
            if status.get("status") == "applied":
                return status
        return None

    wait_for("identity recovery Frigate synchronization", binding_applied, timeout=180)
    camera_key = _frigate_camera_key(camera_uuid)
    aliases = (f"{camera_key}_record", f"{camera_key}_detect")

    def saved_configuration_ready() -> dict[str, object] | None:
        try:
            saved = frigate_saved_config()
        except Exception:
            return None
        cameras = saved.get("cameras") or {}
        streams = (saved.get("go2rtc") or {}).get("streams") or {}
        if camera_key not in cameras or not all(alias in streams for alias in aliases):
            return None
        return saved

    saved = wait_for(
        "saved Frigate identity recovery configuration",
        saved_configuration_ready,
        timeout=180,
        interval=2,
    )

    def frigate_processing() -> bool:
        stats = _frigate_api_json("/api/stats")
        camera_stats = (stats.get("cameras") or {}).get(camera_key) or {}
        return float(camera_stats.get("camera_fps") or 0) > 0

    wait_for("Frigate processing before identity move", frigate_processing, timeout=120, interval=2)
    original_source_fingerprint = _stream_fingerprint(
        f"rtsp://{ONVIF_ORIGINAL_IP}:8554/main"
    )

    def frigate_original_frame() -> list[float] | None:
        fingerprint = _frigate_latest_fingerprint(camera_key)
        if fingerprint is None or fingerprint_distance(
            fingerprint, original_source_fingerprint
        ) > 8:
            return None
        return fingerprint

    frigate_fingerprint = wait_for(
        "Frigate frame from the original identity source",
        frigate_original_frame,
        timeout=90,
        interval=2,
    )

    directory = consumer_directory()
    consumer_camera = _camera_by_id(directory, camera_uuid)
    if consumer_camera is None or consumer_camera.get("state") != "online":
        raise ScenarioFailure("ONVIF identity fixture is absent from the consumer directory")
    signature = directory_signature(directory).get(camera_uuid)
    if not signature:
        raise ScenarioFailure("ONVIF identity fixture has no stable consumer signature")
    identity_history = request_json(f"/internal/cameras/{camera_uuid}/identity-history")
    periods = identity_history.get("periods") or []
    if len(periods) != 1:
        raise ScenarioFailure(f"Initial camera identity history was unexpected: {periods}")
    _assert_identity_period(
        periods[0],
        ip=ONVIF_ORIGINAL_IP,
        mac=ONVIF_ORIGINAL_MAC,
        endpoint=ONVIF_ENDPOINT,
        current=True,
    )
    if periods[0].get("ended_at") is not None:
        raise ScenarioFailure("Initial camera identity period is already closed")
    record_stream = next(
        (
            stream
            for stream in consumer_camera.get("streams", [])
            if "record" in stream.get("roles", [])
        ),
        None,
    )
    if record_stream is None:
        raise ScenarioFailure("ONVIF identity fixture has no recording stream")
    baseline = load_state()
    control_camera = _camera_by_id(directory, str(baseline["open_camera_uuid"]))
    if (
        control_camera is None
        or control_camera.get("state") != "online"
        or not control_camera.get("streams")
    ):
        raise ScenarioFailure("Identity recovery E2E has no second movable camera")
    control_stream = next(
        (
            stream
            for stream in control_camera.get("streams", [])
            if "record" in stream.get("roles", [])
        ),
        control_camera["streams"][0],
    )
    IDENTITY_CONTROL_CONFIG_PATH.write_text(
        json.dumps({"consumer_url": authenticated_rtsp_url(control_stream)}),
        encoding="utf-8",
    )
    IDENTITY_CONTROL_CONFIG_PATH.chmod(0o600)
    streams = (saved.get("go2rtc") or {}).get("streams") or {}
    state = {
        "candidate_uuid": candidate_uuid,
        "camera_uuid": camera_uuid,
        "endpoint_reference": ONVIF_ENDPOINT,
        "ip": ONVIF_ORIGINAL_IP,
        "mac": ONVIF_ORIGINAL_MAC,
        "scan_id": discovery().get("scan_id"),
        "consumer_signature": signature,
        "consumer_url": authenticated_rtsp_url(record_stream),
        "frigate_target_id": target_id,
        "frigate_camera_key": camera_key,
        "frigate_camera": (saved.get("cameras") or {})[camera_key],
        "frigate_streams": {alias: streams[alias] for alias in aliases},
        "frigate_original_fingerprint": frigate_fingerprint,
        "control_camera_uuid": control_camera["id"],
        "control_stream_id": control_stream["id"],
        "control_original_ip": "172.30.0.12",
        "control_moved_ip": OPEN_SECOND_MOVED_IP,
    }
    IDENTITY_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    IDENTITY_STATE_PATH.chmod(0o600)
    print("identity-recovery-setup: stable ONVIF and Frigate identities saved")


def identity_consumer_ready() -> None:
    state = _load_identity_state()

    def ready() -> dict[str, object] | None:
        status = _identity_consumer_status()
        if (
            status.get("status") != "running"
            or int(status.get("frames") or 0) < 5
            or not status.get("fingerprint")
        ):
            return None
        return status

    consumer = wait_for("long-lived downstream consumer", ready, timeout=90)

    def control_ready() -> dict[str, object] | None:
        status = _consumer_status(IDENTITY_CONTROL_STATUS_PATH)
        if (
            status.get("status") != "running"
            or int(status.get("frames") or 0) < 5
            or not status.get("fingerprint")
        ):
            return None
        return status

    control = wait_for(
        "unrelated downstream control consumer",
        control_ready,
        timeout=90,
    )
    state.update(
        {
            "consumer_session_id": consumer["session_id"],
            "consumer_pid": consumer["consumer_pid"],
            "consumer_container_pid": consumer["container_pid"],
            "consumer_wrapper_id": consumer["wrapper_id"],
            "consumer_url_sha256": consumer["url_sha256"],
            "consumer_attempt": consumer["attempt"],
            "consumer_frames_before_move": consumer["frames"],
            "consumer_original_fingerprint": consumer["fingerprint"],
            "control_session_id": control["session_id"],
            "control_pid": control["consumer_pid"],
            "control_container_pid": control["container_pid"],
            "control_wrapper_id": control["wrapper_id"],
            "control_url_sha256": control["url_sha256"],
        }
    )
    IDENTITY_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    IDENTITY_STATE_PATH.chmod(0o600)
    print("identity-consumer-ready: reconnectable downstream client is receiving original media")


def identity_outage_start() -> None:
    state = _load_identity_state()
    state["outage_started_at"] = time.time()
    IDENTITY_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    IDENTITY_STATE_PATH.chmod(0o600)
    print("identity-outage-start: recovery deadline started before camera shutdown")


def identity_recovery_missed_scan() -> None:
    before = _load_identity_state()
    observation: dict[str, object] = {}

    def missed_scan() -> dict[str, object] | None:
        scan = discovery()
        device = next(
            (
                item
                for item in scan.get("devices", [])
                if str(item.get("candidate_uuid")) == str(before["candidate_uuid"])
            ),
            None,
        )
        raw_log = "\n".join(str(line) for line in scan.get("raw_log", []))
        observation.clear()
        observation.update(
            {
                "scan_id": scan.get("scan_id"),
                "scanner": (scan.get("scanners") or {}).get("recovery"),
                "camera_status": device.get("status") if device else None,
                "raw_log": raw_log[-500:],
            }
        )
        if (
            scan.get("scan_id") == before.get("scan_id")
            or (scan.get("scanners") or {}).get("recovery") != "complete"
            or device is None
            or device.get("status") != "offline"
            or f"RECOVERY: target candidate {before['candidate_uuid']}" not in raw_log
        ):
            return None
        return scan

    try:
        missed = wait_for(
            "a targeted recovery scan while the camera is still rebooting",
            missed_scan,
            timeout=35,
            interval=1,
        )
    except ScenarioFailure as exc:
        raise ScenarioFailure(
            f"{exc}; last observation={json.dumps(observation, sort_keys=True)}"
        ) from exc
    before["missed_recovery_scan_id"] = missed.get("scan_id")
    expected_camera_ids = {
        str(before["camera_uuid"]),
        str(before["control_camera_uuid"]),
    }

    def both_offline_incidents_open() -> bool:
        opened = incidents("open").get("incidents") or []
        offline_ids = {
            str(incident.get("camera_id"))
            for incident in opened
            if incident.get("kind") == "media_offline"
        }
        return expected_camera_ids.issubset(offline_ids)

    wait_for(
        "offline incidents for both rebooting cameras",
        both_offline_incidents_open,
        timeout=90,
        interval=2,
    )
    IDENTITY_STATE_PATH.write_text(json.dumps(before), encoding="utf-8")
    IDENTITY_STATE_PATH.chmod(0o600)
    print("identity-recovery-missed-scan: first targeted scan missed the rebooting camera")


def identity_reconnect_checkpoint() -> None:
    state = _load_identity_state()
    state["reconnect_not_before"] = time.time()
    IDENTITY_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    IDENTITY_STATE_PATH.chmod(0o600)
    print("identity-reconnect-checkpoint: stable client URLs saved before recovery")


def identity_recovery() -> None:
    wait_for_open_camera_sources("camera-open-moved-again")
    wait_for_camera_sources(
        "moved ONVIF identity source readiness",
        {
            f"rtsp://{ONVIF_MOVED_IP}:8554/main": ONVIF_MOVED_MAIN_SIZE,
            f"rtsp://{ONVIF_MOVED_IP}:8554/sub": ONVIF_MOVED_SUB_SIZE,
        },
    )
    before = _load_identity_state()
    moved_fingerprint = _stream_fingerprint(f"rtsp://{ONVIF_MOVED_IP}:8554/main")
    original_fingerprint = before["consumer_original_fingerprint"]
    source_separation = fingerprint_distance(original_fingerprint, moved_fingerprint)
    if source_separation < 12:
        raise ScenarioFailure(
            "Original and moved ONVIF fixtures are not visually distinguishable "
            f"(fingerprint distance {source_separation:.2f})"
        )
    observation: dict[str, object] = {}

    def automatically_recovered() -> dict[str, object] | None:
        scan = discovery()
        device = next(
            (
                item
                for item in scan.get("devices", [])
                if str(item.get("candidate_uuid")) == str(before["candidate_uuid"])
            ),
            None,
        )
        adoption = device.get("adoption") if device else None
        streams = (adoption or {}).get("streams") or []
        control_device = next(
            (
                item
                for item in scan.get("devices", [])
                if str(item.get("candidate_uuid")) == "candidate-open"
            ),
            None,
        )
        control_adoption = control_device.get("adoption") if control_device else None
        control_streams = (control_adoption or {}).get("streams") or []
        observation.clear()
        observation.update(
            {
                "scan_id": scan.get("scan_id"),
                "scanner": (scan.get("scanners") or {}).get("recovery"),
                "ip": device.get("ip") if device else None,
                "mac": device.get("mac") if device else None,
                "camera_uuid": (adoption or {}).get("camera_uuid"),
                "sources": [stream.get("uri") for stream in streams],
                "health": [stream.get("health_status") for stream in streams],
                "control_ip": control_device.get("ip") if control_device else None,
                "control_sources": [stream.get("uri") for stream in control_streams],
                "control_health": [
                    stream.get("health_status") for stream in control_streams
                ],
            }
        )
        endpoint = str((device.get("onvif") or {}).get("endpoint_reference") or "").lower() if device else ""
        raw_log = "\n".join(str(line) for line in scan.get("raw_log", []))
        if (
            device is None
            or scan.get("scan_id") == before.get("scan_id")
            or (scan.get("scanners") or {}).get("recovery") != "complete"
            or f"matched {before['candidate_uuid']} by ONVIF endpoint identity" not in raw_log
            or device.get("status") != "online"
            or str(device.get("ip")) != ONVIF_MOVED_IP
            or str(device.get("mac") or "").lower() != ONVIF_MOVED_MAC
            or endpoint != ONVIF_ENDPOINT
            or not adoption
            or str(adoption.get("camera_uuid")) != str(before["camera_uuid"])
            or not streams
            or any(
                urllib.parse.urlsplit(str(stream.get("uri") or "")).hostname
                != ONVIF_MOVED_IP
                or stream.get("health_status") != "healthy"
                for stream in streams
            )
            or control_device is None
            or control_device.get("status") != "online"
            or str(control_device.get("ip")) != OPEN_SECOND_MOVED_IP
            or not control_streams
            or any(
                urllib.parse.urlsplit(str(stream.get("uri") or "")).hostname
                != OPEN_SECOND_MOVED_IP
                or stream.get("health_status") != "healthy"
                for stream in control_streams
            )
        ):
            return None
        return device

    try:
        recovered = wait_for(
            "automatic two-camera identity address recovery",
            automatically_recovered,
            timeout=240,
            interval=2,
        )
    except ScenarioFailure as exc:
        raise ScenarioFailure(
            f"{exc}; last observation={json.dumps(observation, sort_keys=True)}"
        ) from exc
    if observation.get("scan_id") == before.get("missed_recovery_scan_id"):
        raise ScenarioFailure("Camera recovery did not require a targeted scan retry")

    consumer_observation: dict[str, object] = {}

    def reconnected_consumer_received_moved_source() -> dict[str, object] | None:
        status = _identity_consumer_status()
        consumer_observation.clear()
        consumer_observation.update(status)
        if (
            status.get("status") != "running"
            or status.get("session_id") == before.get("consumer_session_id")
            or status.get("consumer_pid") == before.get("consumer_pid")
            or status.get("container_pid") != before.get("consumer_container_pid")
            or status.get("wrapper_id") != before.get("consumer_wrapper_id")
            or status.get("url_sha256") != before.get("consumer_url_sha256")
            or int(status.get("attempt") or 0)
            <= int(before.get("consumer_attempt") or 0)
            or int(status.get("frames") or 0) < 5
            or not status.get("fingerprint")
        ):
            return None
        moved_distance = fingerprint_distance(status["fingerprint"], moved_fingerprint)
        original_distance = fingerprint_distance(
            status["fingerprint"], original_fingerprint
        )
        consumer_observation.update(
            {
                "moved_distance": round(moved_distance, 3),
                "original_distance": round(original_distance, 3),
                "source_separation": round(source_separation, 3),
            }
        )
        if (
            moved_distance > max(8, source_separation * 0.35)
            or moved_distance >= original_distance
        ):
            return None
        return status

    try:
        switched = wait_for(
            "a new downstream session to reconnect to moved media",
            reconnected_consumer_received_moved_source,
            timeout=120,
            interval=1,
        )
    except ScenarioFailure as exc:
        raise ScenarioFailure(
            f"{exc}; last consumer={json.dumps(consumer_observation, sort_keys=True)}"
        ) from exc
    transition = wait_for(
        "immutable decoded-frame evidence from the moved camera",
        lambda: _identity_moved_frame_transition(
            outage_started_at=float(before["outage_started_at"]),
            reconnect_not_before=float(before["reconnect_not_before"]),
            original_session_id=str(before["consumer_session_id"]),
            moved_session_id=str(switched["session_id"]),
            original_fingerprint=original_fingerprint,
            moved_fingerprint=moved_fingerprint,
            source_separation=source_separation,
        ),
        timeout=10,
        interval=0.2,
    )
    reconnect_seconds = float(transition["first_moved_frame_at"]) - float(
        before["reconnect_not_before"]
    )
    if reconnect_seconds >= 45:
        raise ScenarioFailure(
            "Live media reconnect exceeded the recovery budget "
            f"({reconnect_seconds:.1f}s >= 45s)"
        )

    switched_frames = int(switched.get("frames") or 0)

    def reconnected_consumer_remains_fresh() -> dict[str, object] | None:
        status = _identity_consumer_status()
        if (
            status.get("status") != "running"
            or status.get("session_id") != switched.get("session_id")
            or status.get("consumer_pid") != switched.get("consumer_pid")
            or status.get("container_pid") != switched.get("container_pid")
            or status.get("wrapper_id") != switched.get("wrapper_id")
            or status.get("url_sha256") != switched.get("url_sha256")
            or int(status.get("frames") or 0) <= switched_frames + 5
            or not status.get("fingerprint")
        ):
            return None
        moved_distance = fingerprint_distance(status["fingerprint"], moved_fingerprint)
        original_distance = fingerprint_distance(
            status["fingerprint"], original_fingerprint
        )
        if (
            moved_distance > max(8, source_separation * 0.35)
            or moved_distance >= original_distance
        ):
            return None
        return status

    wait_for(
        "the reconnected downstream session to keep receiving fresh moved media",
        reconnected_consumer_remains_fresh,
        timeout=30,
        interval=1,
    )

    directory = consumer_directory()
    camera_uuid = str(before["camera_uuid"])
    if directory_signature(directory).get(camera_uuid) != before["consumer_signature"]:
        raise ScenarioFailure("ONVIF address recovery changed consumer camera or stream identity")
    consumer_camera = _camera_by_id(directory, camera_uuid)
    if consumer_camera is None or consumer_camera.get("state") != "online":
        raise ScenarioFailure("Recovered ONVIF camera is not online for consumers")
    record_stream = next(
        (
            stream
            for stream in consumer_camera.get("streams", [])
            if "record" in stream.get("roles", [])
        ),
        None,
    )
    if record_stream is None:
        raise ScenarioFailure(
            "Recovered consumer recording stream no longer maps to the main profile"
        )
    if authenticated_rtsp_url(record_stream) != before["consumer_url"]:
        raise ScenarioFailure("Automatic camera recovery changed the downstream RTSP URL")
    record_video = record_stream.get("video") or {}
    if (
        int(record_video.get("width") or 0),
        int(record_video.get("height") or 0),
    ) != ONVIF_MOVED_MAIN_SIZE:
        raise ScenarioFailure(
            "Recovered consumer metadata did not update to the moved camera format"
        )
    for stream in consumer_camera.get("streams", []):
        video = probe_stream(stream)
        if stream is record_stream and (
            (int(video.get("width") or 0), int(video.get("height") or 0))
            != ONVIF_MOVED_MAIN_SIZE
        ):
            raise ScenarioFailure(
                "Recovered recording stream does not decode the main profile"
            )
    assert_snapshot(camera_uuid)

    camera_key = str(before["frigate_camera_key"])
    expected_streams = before["frigate_streams"]
    expected_camera = before["frigate_camera"]

    def recovered_frigate_config() -> dict[str, object] | None:
        saved_config = frigate_saved_config()
        camera_config = (saved_config.get("cameras") or {}).get(camera_key)
        if not isinstance(camera_config, dict) or camera_config.get("detect") != {
            "width": ONVIF_MOVED_SUB_SIZE[0],
            "height": ONVIF_MOVED_SUB_SIZE[1],
        }:
            return None
        return saved_config

    saved = wait_for(
        "Frigate saved config after identity move",
        recovered_frigate_config,
        timeout=90,
        interval=2,
    )
    saved_camera = (saved.get("cameras") or {}).get(camera_key)
    saved_streams = (saved.get("go2rtc") or {}).get("streams") or {}
    if not isinstance(saved_camera, dict) or not isinstance(expected_camera, dict):
        raise ScenarioFailure("Automatic camera recovery lost the Frigate camera config")
    saved_camera_without_detect = {
        key: value for key, value in saved_camera.items() if key != "detect"
    }
    expected_camera_without_detect = {
        key: value for key, value in expected_camera.items() if key != "detect"
    }
    if saved_camera_without_detect != expected_camera_without_detect:
        raise ScenarioFailure(
            "Automatic camera recovery changed stable Frigate camera settings"
        )
    if {alias: saved_streams.get(alias) for alias in expected_streams} != expected_streams:
        raise ScenarioFailure("Automatic camera recovery changed Frigate stream URLs")

    def frigate_resumed() -> bool:
        stats = _frigate_api_json("/api/stats")
        camera_stats = (stats.get("cameras") or {}).get(camera_key) or {}
        return float(camera_stats.get("camera_fps") or 0) > 0

    wait_for("Frigate processing after identity move", frigate_resumed, timeout=180, interval=2)
    wait_for_camera_sources(
        "Frigate restream with replacement RTSP metadata",
        {
            f"rtsp://172.30.0.30:8554/{camera_key}_record": ONVIF_MOVED_MAIN_SIZE,
        },
    )
    frigate_original_fingerprint = before["frigate_original_fingerprint"]

    def frigate_moved_frame() -> list[float] | None:
        fingerprint = _frigate_latest_fingerprint(camera_key)
        if fingerprint is None:
            return None
        moved_distance = fingerprint_distance(fingerprint, moved_fingerprint)
        original_distance = fingerprint_distance(
            fingerprint, frigate_original_fingerprint
        )
        if (
            moved_distance > max(8, source_separation * 0.35)
            or moved_distance >= original_distance
        ):
            return None
        return fingerprint

    wait_for(
        "Frigate frame from the moved identity source",
        frigate_moved_frame,
        timeout=120,
        interval=2,
    )

    def identity_history_updated() -> list[dict[str, object]] | None:
        history = request_json(
            f"/internal/cameras/{camera_uuid}/identity-history"
        ).get("periods") or []
        return history if len(history) == 2 else None

    periods = wait_for(
        "camera identity history after address recovery",
        identity_history_updated,
        timeout=60,
    )
    _assert_identity_period(
        periods[0],
        ip=ONVIF_MOVED_IP,
        mac=ONVIF_MOVED_MAC,
        endpoint=ONVIF_ENDPOINT,
        current=True,
    )
    _assert_identity_period(
        periods[1],
        ip=ONVIF_ORIGINAL_IP,
        mac=ONVIF_ORIGINAL_MAC,
        endpoint=ONVIF_ENDPOINT,
        current=False,
    )
    if periods[0].get("ended_at") is not None or not periods[1].get("ended_at"):
        raise ScenarioFailure("Recovered identity periods do not have one current period")
    if str(recovered.get("candidate_uuid")) != str(before["candidate_uuid"]):
        raise ScenarioFailure("Automatic recovery created a replacement candidate identity")

    expected_camera_ids = {
        str(before["camera_uuid"]),
        str(before["control_camera_uuid"]),
    }

    def recovery_incidents_resolved() -> bool:
        resolved = incidents("resolved").get("incidents") or []
        states = {
            (str(incident.get("camera_id")), str(incident.get("kind")))
            for incident in resolved
            if incident.get("resolution_reason") in {"recovered", "stream_recovered"}
        }
        return all(
            (camera_id, kind) in states
            for camera_id in expected_camera_ids
            for kind in ("media_offline", "camera_address_changed")
        )

    wait_for(
        "resolved offline and address-change incidents for both cameras",
        recovery_incidents_resolved,
        timeout=60,
        interval=2,
    )
    print(
        "identity-recovery: two camera addresses recovered through targeted retry scans, "
        "stable identities and consumer URLs were preserved; "
        f"first moved frame decoded in {reconnect_seconds:.1f}s "
        f"(maximum frame gap {float(transition['maximum_frame_gap']):.1f}s)"
    )


def identity_runtime_restart() -> None:
    before = _load_identity_state()
    moved_fingerprint = _stream_fingerprint(f"rtsp://{ONVIF_MOVED_IP}:8554/main")
    original_fingerprint = before["consumer_original_fingerprint"]
    source_separation = fingerprint_distance(original_fingerprint, moved_fingerprint)
    observation: dict[str, object] = {}

    def persisted_source_resumed() -> bool:
        try:
            fingerprint = _stream_fingerprint(str(before["consumer_url"]))
        except ScenarioFailure as exc:
            observation.clear()
            observation["error"] = str(exc)
            return False
        moved_distance = fingerprint_distance(fingerprint, moved_fingerprint)
        original_distance = fingerprint_distance(fingerprint, original_fingerprint)
        observation.clear()
        observation.update(
            {
                "moved_distance": round(moved_distance, 3),
                "original_distance": round(original_distance, 3),
                "source_separation": round(source_separation, 3),
            }
        )
        return (
            moved_distance <= max(8, source_separation * 0.35)
            and moved_distance < original_distance
        )

    try:
        wait_for(
            "the persisted moved source after go2rtc child restart",
            persisted_source_resumed,
            timeout=120,
            interval=1,
        )
    except ScenarioFailure as exc:
        raise ScenarioFailure(
            f"{exc}; last observation={json.dumps(observation, sort_keys=True)}"
        ) from exc

    camera_uuid = str(before["camera_uuid"])
    directory = consumer_directory()
    if directory_signature(directory).get(camera_uuid) != before["consumer_signature"]:
        raise ScenarioFailure("go2rtc child restart changed the consumer stream identity")
    print(
        "identity-runtime-restart: moved source persisted across a supervised go2rtc "
        "child restart without restarting CamAdmiral"
    )


def identity_replacement() -> None:
    wait_for_camera_sources(
        "replacement ONVIF identity source readiness",
        {
            f"rtsp://{ONVIF_REPLACEMENT_IP}:8554/main": ONVIF_MOVED_MAIN_SIZE,
            f"rtsp://{ONVIF_REPLACEMENT_IP}:8554/sub": ONVIF_MOVED_SUB_SIZE,
        },
    )
    before = _load_identity_state()
    network_configuration = request_json("/internal/discovery/networks")
    selected_before = [
        str(network["cidr"])
        for network in network_configuration.get("networks", [])
        if network.get("selected")
    ]
    custom_before = [
        str(network["cidr"])
        for network in network_configuration.get("networks", [])
        if network.get("source") == "custom"
    ]
    request_json(
        "/internal/discovery/networks",
        method="PUT",
        headers={"X-CamAdmiral-Action": "save-discovery-networks"},
        payload={
            "selected_subnets": ["172.30.0.0/24"],
            "custom_subnets": custom_before,
        },
    )
    try:
        def start_full_scan() -> dict[str, object] | None:
            status, body, _headers = request(
                "/internal/discovery/scan",
                method="POST",
                headers={"X-CamAdmiral-Action": "scan"},
                timeout=15,
            )
            if status == 409:
                return None
            try:
                payload = json.loads(body)
            except (ValueError, json.JSONDecodeError) as exc:
                raise ScenarioFailure(
                    f"Identity replacement scan returned invalid JSON with HTTP {status}"
                ) from exc
            if status != 202:
                raise ScenarioFailure(
                    f"Identity replacement scan returned HTTP {status}: {payload}"
                )
            return payload

        queued = wait_for(
            "identity replacement full scan start",
            start_full_scan,
            timeout=90,
            interval=2,
        )
        scan_id = str(queued.get("scan_id") or "")
        if not scan_id:
            raise ScenarioFailure("Identity replacement scan has no scan identity")

        def scan_complete() -> dict[str, object] | None:
            scan = discovery()
            if scan.get("scan_id") != scan_id or scan.get("status") in {"queued", "running"}:
                return None
            if scan.get("status") != "complete":
                raise ScenarioFailure(f"Identity replacement scan failed: {scan}")
            return scan

        scanned = wait_for(
            "identity replacement full scan completion",
            scan_complete,
            timeout=180,
            interval=2,
        )
    finally:
        request_json(
            "/internal/discovery/networks",
            method="PUT",
            headers={"X-CamAdmiral-Action": "save-discovery-networks"},
            payload={
                "selected_subnets": selected_before,
                "custom_subnets": custom_before,
            },
        )

    old_candidate = next(
        (
            device
            for device in scanned.get("devices", [])
            if str(device.get("candidate_uuid")) == str(before["candidate_uuid"])
        ),
        None,
    )
    new_candidate = next(
        (
            device
            for device in scanned.get("devices", [])
            if str((device.get("onvif") or {}).get("endpoint_reference") or "").lower()
            == ONVIF_REPLACEMENT_ENDPOINT
        ),
        None,
    )
    old_endpoint = str(
        ((old_candidate or {}).get("onvif") or {}).get("endpoint_reference") or ""
    ).lower()
    old_camera_uuid = str(
        ((old_candidate or {}).get("adoption") or {}).get("camera_uuid") or ""
    )
    if (
        old_candidate is None
        or old_candidate.get("status") != "offline"
        or str(old_candidate.get("ip")) != ONVIF_MOVED_IP
        or str(old_candidate.get("mac") or "").lower() != ONVIF_MOVED_MAC
        or old_endpoint != ONVIF_ENDPOINT
        or old_camera_uuid != str(before["camera_uuid"])
    ):
        raise ScenarioFailure(f"Old ONVIF identity was not retained offline: {old_candidate}")
    if (
        new_candidate is None
        or new_candidate.get("status") != "online"
        or str(new_candidate.get("ip")) != ONVIF_REPLACEMENT_IP
        or str(new_candidate.get("mac") or "").lower() != ONVIF_REPLACEMENT_MAC
        or str(new_candidate.get("candidate_uuid")) == str(before["candidate_uuid"])
        or new_candidate.get("adoption")
    ):
        raise ScenarioFailure(f"New ONVIF identity was not discovered separately: {new_candidate}")

    new_candidate_uuid = str(new_candidate["candidate_uuid"])
    adopted = request_json(
        f"/internal/discovery/{urllib.parse.quote(new_candidate_uuid, safe='')}/adopt",
        method="POST",
        headers={"X-CamAdmiral-Action": "adopt"},
        payload={"username": "", "password": "", "allow_factory_credentials": False},
        timeout=120,
    )
    new_camera_uuid = str((adopted.get("adoption") or {}).get("camera_uuid") or "")
    if not new_camera_uuid or new_camera_uuid == str(before["camera_uuid"]):
        raise ScenarioFailure("Reidentified ONVIF camera did not receive a new camera identity")

    def replacement_online() -> dict[str, object] | None:
        directory = consumer_directory()
        old_camera = _camera_by_id(directory, str(before["camera_uuid"]))
        new_camera = _camera_by_id(directory, new_camera_uuid)
        if (
            old_camera is None
            or old_camera.get("state") not in {"degraded", "offline"}
            or new_camera is None
            or new_camera.get("state") != "online"
            or not new_camera.get("streams")
        ):
            return None
        return new_camera

    replacement = wait_for(
        "adopted replacement ONVIF camera",
        replacement_online,
        timeout=120,
        interval=2,
    )
    for stream in replacement.get("streams", []):
        probe_stream(stream)
    assert_snapshot(new_camera_uuid)

    saved = frigate_saved_config()
    old_camera_key = str(before["frigate_camera_key"])
    new_camera_key = _frigate_camera_key(new_camera_uuid)
    cameras = saved.get("cameras") or {}
    if old_camera_key not in cameras:
        raise ScenarioFailure("Frigate silently removed the old offline camera identity")
    if new_camera_key in cameras:
        raise ScenarioFailure("Frigate silently synchronized the replacement camera identity")
    print(
        "identity-replacement: changed IP, MAC, and ONVIF identity produced an offline old "
        "camera and an independently adoptable replacement"
    )


SCENARIOS = {
    "baseline": baseline,
    "direct-rtsp-created": direct_rtsp_created,
    "direct-rtsp-frigate": direct_rtsp_frigate,
    "direct-rtsp-after-restart": direct_rtsp_after_restart,
    "direct-rtsp-dns-move": direct_rtsp_dns_move,
    "direct-rtsp-path-failure": direct_rtsp_path_failure,
    "direct-rtsp-path-recovery": direct_rtsp_path_recovery,
    "multi-subnet-discovery": multi_subnet_discovery,
    "partial-subnet-preservation": partial_subnet_preservation,
    "large-subnet-multicast-discovery": large_subnet_multicast_discovery,
    "configured-routed-subnet-discovery": configured_routed_subnet_discovery,
    "runtime-drift": runtime_drift,
    "runtime-recovery": runtime_recovery,
    "camera-outage": camera_outage,
    "camera-recovery": camera_recovery,
    "container-restart": container_restart,
    "address-recovery": address_recovery,
    "moved-camera-ready": moved_camera_ready,
    "accept-ui-lifecycle-state": accept_ui_lifecycle_state,
    "invalid-address": invalid_address,
    "rotated-camera-ready": rotated_camera_ready,
    "credential-repair": credential_repair,
    "frigate": frigate,
    "frigate-restart-verify": frigate_restart_verify,
    "frigate-unadopt": frigate_unadopt,
    "frigate-ambiguous-delete-setup": frigate_ambiguous_delete_setup,
    "frigate-ambiguous-delete-verify": frigate_ambiguous_delete_verify,
    "identity-recovery-setup": identity_recovery_setup,
    "identity-consumer-ready": identity_consumer_ready,
    "identity-outage-start": identity_outage_start,
    "identity-recovery-missed-scan": identity_recovery_missed_scan,
    "identity-reconnect-checkpoint": identity_reconnect_checkpoint,
    "identity-recovery": identity_recovery,
    "identity-runtime-restart": identity_runtime_restart,
    "identity-replacement": identity_replacement,
    "relay-latency": relay_latency,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in SCENARIOS:
        print("usage: scenarios.py " + "|".join(SCENARIOS), file=sys.stderr)
        return 2
    try:
        SCENARIOS[sys.argv[1]]()
    except Exception as exc:
        print(f"E2E FAILED [{sys.argv[1]}]: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
