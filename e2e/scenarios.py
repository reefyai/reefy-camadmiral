from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


BASE_URL = "http://camadmiral:18080"
API_TOKEN = "synthetic-e2e-api-token"
STATE_PATH = Path("/state/baseline.json")
OPEN_NAME = "Synthetic open camera"
AUTH_NAME = "Synthetic authenticated camera"
ONVIF_NAME = "Synthetic ONVIF camera"
ADMIN_PASSWORD = os.environ.get("CAMADMIRAL_E2E_ADMIN_PASSWORD", "")


class ScenarioFailure(RuntimeError):
    pass


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
    print("baseline: adoption, roles, auth rejection, lifecycle, media, snapshots, fan-out, and alert contracts passed")


def load_state() -> dict[str, object]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ScenarioFailure("E2E baseline state is unavailable") from exc


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
    print("camera-recovery: synthetic camera reboot recovered without user action")


def container_restart() -> None:
    wait_for_health()
    assert_stable(load_state())
    print("container-restart: persistent identities, secrets, and streams passed")


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
        return bool(streams) and all(
            urllib.parse.urlsplit(stream.get("uri", "")).hostname == "172.30.0.12"
            and stream.get("health_status") == "healthy"
            for stream in streams
        )

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

    def frigate_json(path: str) -> dict[str, object]:
        try:
            with urllib.request.urlopen(f"http://camadmiral:5000{path}", timeout=8) as response:
                return json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return {}

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
        r"[^a-zA-Z0-9_]", "_", str(state["open_camera_uuid"])
    )
    if not config["cameras"][camera_key].get("live", {}).get("streams", {}):
        raise ScenarioFailure("Frigate camera has no live stream mapping")

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
    print("frigate: real 0.17 API injection and camera processing passed")


SCENARIOS = {
    "baseline": baseline,
    "runtime-drift": runtime_drift,
    "runtime-recovery": runtime_recovery,
    "camera-outage": camera_outage,
    "camera-recovery": camera_recovery,
    "container-restart": container_restart,
    "address-recovery": address_recovery,
    "moved-camera-ready": moved_camera_ready,
    "invalid-address": invalid_address,
    "rotated-camera-ready": rotated_camera_ready,
    "credential-repair": credential_repair,
    "frigate": frigate,
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
