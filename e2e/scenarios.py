from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


BASE_URL = "http://camadmiral:18080"
API_TOKEN = "synthetic-e2e-api-token"
STATE_PATH = Path("/state/baseline.json")
OPEN_NAME = "Synthetic open camera"
AUTH_NAME = "Synthetic authenticated camera"


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
    status, body, headers = request(f"/internal/cameras/{camera_uuid}/snapshot.jpg", timeout=20)
    if status != 200 or not body.startswith(b"\xff\xd8\xff") or not body.endswith(b"\xff\xd9"):
        raise ScenarioFailure("Camera snapshot is not a valid JPEG")
    content_type = next(
        (value for name, value in headers.items() if name.lower() == "content-type"),
        "",
    )
    if not content_type.startswith("image/jpeg"):
        raise ScenarioFailure("Camera snapshot has an unexpected content type")


def assert_all_media(directory: dict[str, object]) -> None:
    for camera in directory.get("cameras", []):
        for stream in camera.get("streams", []):
            probe_stream(stream)
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
    def stable_media() -> dict[str, object] | None:
        directory = consumer_directory()
        cameras = {camera.get("name"): camera for camera in directory.get("cameras", [])}
        if not {OPEN_NAME, AUTH_NAME}.issubset(cameras):
            return None
        if any(cameras[name].get("state") != "online" for name in {OPEN_NAME, AUTH_NAME}):
            return None
        if directory_signature(directory) != state["signature"]:
            raise ScenarioFailure("Stable camera, stream, or downstream identity changed")
        assert_all_media(directory)
        return directory

    return wait_for("stable identities and decoded media", stable_media, timeout=120)


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
        "8",
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

        wait_for("two downstream clients sharing one camera session", one_upstream, timeout=12, interval=0.25)
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
    assert_version_surface()
    wait_for(
        "seeded discovery inventory",
        lambda: discovery_device("candidate-open") and discovery_device("candidate-auth"),
    )
    status, _body, _headers = request("/api/v1/cameras")
    if status != 401:
        raise ScenarioFailure("Consumer API accepted a missing bearer token")
    consumer_directory(token="invalid-synthetic-token", expected=401)

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

    directory = wait_for_online({OPEN_NAME, AUTH_NAME})
    assert_all_media(directory)
    open_camera = camera_by_name(directory, OPEN_NAME)
    auth_camera = camera_by_name(directory, AUTH_NAME)
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
    request_json(
        f'/internal/cameras/{open_camera["id"]}/enabled',
        method="POST",
        headers={"X-CamAdmiral-Action": "set-camera-enabled"},
        payload={"enabled": True},
        timeout=120,
    )
    directory = wait_for_online({OPEN_NAME, AUTH_NAME})
    assert_all_media(directory)

    state = {
        "open_camera_uuid": open_camera["id"],
        "auth_camera_uuid": auth_camera["id"],
        "signature": directory_signature(directory),
    }
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    STATE_PATH.chmod(0o600)
    print("baseline: adoption, roles, auth rejection, lifecycle, media, snapshots, and fan-out passed")


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
        return open_camera.get("state") in {"degraded", "offline"} and auth_camera.get("state") == "online"

    wait_for("camera outage health transition", outage_visible, timeout=120)
    print("camera-outage: failure was visible without withdrawing stable downstream identities")


def camera_recovery() -> None:
    wait_for_health()
    assert_stable(load_state())
    print("camera-recovery: synthetic camera reboot recovered without user action")


def container_restart() -> None:
    wait_for_health()
    assert_stable(load_state())
    print("container-restart: persistent identities, secrets, and streams passed")


def address_recovery() -> None:
    state = load_state()

    def moved() -> bool:
        device = discovery_device("candidate-open")
        adoption = device.get("adoption") if device else None
        streams = adoption.get("streams", []) if adoption else []
        return bool(streams) and all(
            urllib.parse.urlsplit(stream.get("uri", "")).hostname == "172.30.0.12"
            and stream.get("health_status") == "healthy"
            for stream in streams
        )

    wait_for("validated camera address promotion", moved, timeout=120)
    assert_stable(state)
    print("address-recovery: upstream moved while downstream identities stayed stable")


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


SCENARIOS = {
    "baseline": baseline,
    "runtime-drift": runtime_drift,
    "runtime-recovery": runtime_recovery,
    "camera-outage": camera_outage,
    "camera-recovery": camera_recovery,
    "container-restart": container_restart,
    "address-recovery": address_recovery,
    "credential-repair": credential_repair,
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
