from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
CAMADMIRAL_RTSP_USERNAME = "camadmiral"
CAMADMIRAL_RTSP_PORT = 18554
FRIGATE_GO2RTC_PORT = 8554
KEY_PREFIX = "camadmiral_"
RUNTIME_CLEANUP_TIMEOUT_SECONDS = 10.0
RUNTIME_CLEANUP_POLL_SECONDS = 0.25
CAMERA_WORKER_CLEANUP_TIMEOUT_SECONDS = 60.0
CAMERA_WORKER_CLEANUP_POLL_SECONDS = 0.5
CAMERA_DYNAMIC_CLEANUP_GRACE_SECONDS = 5.0
FRIGATE_RESTART_SETTLE_SECONDS = 2.0
REQUIRED_CAPABILITIES = {
    "/config": "get",
    "/config/raw": "get",
    "/config/raw_paths": "get",
    "/config/set": "put",
    "/go2rtc/streams": "get",
    "/go2rtc/streams/{stream_name}": "put",
    "/restart": "post",
    "/stats": "get",
}


class FrigateApiError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        stage: str | None = None,
        resource: str | None = None,
        upstream_status: int | None = None,
        upstream_detail: str | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.resource = resource
        self.upstream_status = upstream_status
        self.upstream_detail = upstream_detail

    def with_context(self, *, stage: str, resource: str | None = None) -> FrigateApiError:
        return FrigateApiError(
            self.code,
            stage=self.stage or stage,
            resource=self.resource or resource,
            upstream_status=self.upstream_status,
            upstream_detail=self.upstream_detail,
        )


@dataclass(frozen=True)
class FrigateTarget:
    target_id: str
    name: str
    api_url: str


def normalize_frigate_api_url(value: object) -> str:
    if not isinstance(value, str):
        raise FrigateApiError("invalid_target_url")
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise FrigateApiError("invalid_target_url")
    try:
        port = parsed.port
    except ValueError as exc:
        raise FrigateApiError("invalid_target_url") from exc
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or port is None or not 1 <= port <= 65535:
        raise FrigateApiError("invalid_target_url")
    if parsed.path not in {"", "/"}:
        raise FrigateApiError("invalid_target_url")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"http://{host}:{port}"


def load_frigate_targets(repository: Any) -> list[FrigateTarget]:
    return [
        FrigateTarget(str(target["target_id"]), str(target["name"]), str(target["api_url"]))
        for target in repository.frigate_targets()
    ]


class FrigateClient:
    def __init__(self, target: FrigateTarget, timeout: float = 5.0):
        self.target = target
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.target.api_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_RESPONSE_BYTES:
                    raise FrigateApiError("response_too_large")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except FrigateApiError:
            raise
        except urllib.error.HTTPError as exc:
            detail = self._safe_http_error_detail(exc)
            if exc.code in {401, 403}:
                raise FrigateApiError(
                    "authorization_required",
                    upstream_status=exc.code,
                    upstream_detail=detail,
                ) from exc
            if exc.code == 404:
                raise FrigateApiError(
                    "capability_unavailable",
                    upstream_status=exc.code,
                    upstream_detail=detail,
                ) from exc
            raise FrigateApiError(
                "request_rejected",
                upstream_status=exc.code,
                upstream_detail=detail,
            ) from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise FrigateApiError("target_unavailable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FrigateApiError("response_too_large")
        try:
            return json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise FrigateApiError("invalid_response") from exc

    @staticmethod
    def _safe_http_error_detail(exc: urllib.error.HTTPError) -> str | None:
        try:
            raw = exc.read(4097)
        except OSError:
            return None
        if not raw or len(raw) > 4096:
            return None
        try:
            decoded = raw.decode("utf-8")
        except UnicodeError:
            return None
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError:
            detail = decoded
        else:
            detail = payload.get("message") if isinstance(payload, dict) else None
            if not isinstance(detail, str):
                detail = decoded
        detail = " ".join(detail.split())
        detail = re.sub(
            r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s@]+)@",
            r"\1***@",
            detail,
        )
        return detail[:240] or None

    def capabilities(self) -> None:
        document = self._request("GET", "/api/openapi.json")
        paths = document.get("paths") if isinstance(document, dict) else None
        if not isinstance(paths, dict):
            raise FrigateApiError("capability_unavailable")
        for path, method in REQUIRED_CAPABILITIES.items():
            operations = paths.get(path)
            if not isinstance(operations, dict) or method not in operations:
                raise FrigateApiError("capability_unavailable")

    def config(self) -> dict[str, Any]:
        result = self._request("GET", "/api/config")
        if not isinstance(result, dict):
            raise FrigateApiError("invalid_response")
        return result

    def raw_paths(self) -> dict[str, Any]:
        result = self._request("GET", "/api/config/raw_paths")
        if not isinstance(result, dict):
            raise FrigateApiError("invalid_response")
        return result

    def raw_config(self) -> dict[str, Any]:
        result = self._request("GET", "/api/config/raw")
        if not isinstance(result, str):
            raise FrigateApiError("invalid_response")
        try:
            parsed = yaml.safe_load(result)
        except yaml.YAMLError as exc:
            raise FrigateApiError("invalid_response") from exc
        if not isinstance(parsed, dict):
            raise FrigateApiError("invalid_response")
        return parsed

    def runtime_streams(self) -> dict[str, Any]:
        result = self._request("GET", "/api/go2rtc/streams")
        if not isinstance(result, dict):
            raise FrigateApiError("invalid_response")
        return result

    def stats(self) -> dict[str, Any]:
        result = self._request("GET", "/api/stats")
        if not isinstance(result, dict):
            raise FrigateApiError("invalid_response")
        cameras = result.get("cameras")
        if not isinstance(cameras, dict):
            raise FrigateApiError("invalid_response")
        return cameras

    def set_config(self, config_data: dict[str, Any], *, update_topic: str | None = None) -> None:
        payload: dict[str, Any] = {"requires_restart": 0, "config_data": config_data}
        if update_topic is not None:
            payload["requires_restart"] = 1
            payload["update_topic"] = update_topic
        result = self._request("PUT", "/api/config/set", payload)
        if not isinstance(result, dict) or result.get("success") is not True:
            raise FrigateApiError("configuration_rejected")

    def set_runtime_stream(self, stream_name: str, source: str) -> None:
        encoded_name = urllib.parse.quote(stream_name, safe="")
        query = urllib.parse.urlencode({"src": source})
        result = self._request("PUT", f"/api/go2rtc/streams/{encoded_name}?{query}")
        if isinstance(result, dict) and result.get("success") is False:
            raise FrigateApiError("runtime_stream_rejected")

    def delete_runtime_stream(self, stream_name: str) -> None:
        encoded_name = urllib.parse.quote(stream_name, safe="")
        result = self._request("DELETE", f"/api/go2rtc/streams/{encoded_name}")
        if isinstance(result, dict) and result.get("success") is False:
            raise FrigateApiError("runtime_stream_rejected")

    def restart(self) -> None:
        result = self._request("POST", "/api/restart")
        if not isinstance(result, dict) or result.get("success") is not True:
            raise FrigateApiError("restart_rejected")


def frigate_camera_key(camera_uuid: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]", "_", camera_uuid)
    return f"{KEY_PREFIX}{normalized}"


def selected_camera_inventory(repository: Any, target_id: str) -> list[dict[str, Any]]:
    selected = set(repository.selected_frigate_camera_uuids(target_id))
    if not selected:
        return []
    return [
        camera
        for camera in repository.consumer_inventory()
        if str(camera["camera_uuid"]) in selected
    ]


def desired_resource_keys(repository: Any, target_id: str) -> tuple[set[str], set[str]]:
    camera_keys = {
        frigate_camera_key(str(camera["camera_uuid"]))
        for camera in selected_camera_inventory(repository, target_id)
    }
    stream_keys = {
        stream_key
        for camera_key in camera_keys
        for stream_key in (f"{camera_key}_record", f"{camera_key}_detect")
    }
    return camera_keys, stream_keys


def full_sync_preview(
    repository: Any,
    target: FrigateTarget,
    *,
    client_factory: Callable[[FrigateTarget], FrigateClient] = FrigateClient,
) -> dict[str, Any]:
    client = client_factory(target)
    client.capabilities()
    state = _full_sync_state(repository, client)
    return {
        "managed_cameras": state["managed_cameras"],
        "stale_cameras": state["stale_cameras"],
        "stale_streams": state["stale_streams"],
    }


def _full_sync_state(repository: Any, client: FrigateClient) -> dict[str, Any]:
    saved_config = client.raw_config()
    runtime_streams = client.runtime_streams()
    desired_cameras, desired_streams = desired_resource_keys(repository, client.target.target_id)
    configured_cameras = saved_config.get("cameras", {})
    configured_streams = saved_config.get("go2rtc", {}).get("streams", {})
    if (
        not isinstance(configured_cameras, dict)
        or not isinstance(configured_streams, dict)
        or not isinstance(runtime_streams, dict)
    ):
        raise FrigateApiError("invalid_response")
    stale_cameras = sorted(
        name
        for name in configured_cameras
        if name.startswith(KEY_PREFIX) and name not in desired_cameras
    )
    stale_config_streams = {
        name
        for name in configured_streams
        if name.startswith(KEY_PREFIX) and name not in desired_streams
    }
    stale_runtime_streams = {
        name
        for name in runtime_streams
        if name.startswith(KEY_PREFIX) and name not in desired_streams
    }
    return {
        "managed_cameras": len(desired_cameras),
        "stale_cameras": stale_cameras,
        "stale_config_streams": sorted(stale_config_streams),
        "stale_runtime_streams": sorted(stale_runtime_streams),
        "stale_streams": sorted(stale_config_streams | stale_runtime_streams),
    }


def _wait_for_runtime_cleanup(
    client: FrigateClient,
    stream_keys: list[str],
) -> list[str]:
    deadline = time.monotonic() + RUNTIME_CLEANUP_TIMEOUT_SECONDS
    while True:
        runtime_streams = client.runtime_streams()
        remaining = [
            stream_key for stream_key in stream_keys if stream_key in runtime_streams
        ]
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(RUNTIME_CLEANUP_POLL_SECONDS)


def _wait_for_camera_worker_cleanup(
    client: FrigateClient,
    camera_keys: list[str],
    *,
    timeout: float = CAMERA_WORKER_CLEANUP_TIMEOUT_SECONDS,
) -> list[str]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            stats = client.stats()
        except FrigateApiError:
            if time.monotonic() >= deadline:
                raise
        else:
            remaining = [camera_key for camera_key in camera_keys if camera_key in stats]
            if not remaining or time.monotonic() >= deadline:
                return remaining
        time.sleep(CAMERA_WORKER_CLEANUP_POLL_SECONDS)


def _wait_for_camera_workers(
    client: FrigateClient,
    camera_keys: list[str],
    *,
    timeout: float = CAMERA_WORKER_CLEANUP_TIMEOUT_SECONDS,
) -> list[str]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            stats = client.stats()
        except FrigateApiError:
            if time.monotonic() >= deadline:
                raise
        else:
            missing = [camera_key for camera_key in camera_keys if camera_key not in stats]
            if not missing or time.monotonic() >= deadline:
                return missing
        time.sleep(CAMERA_WORKER_CLEANUP_POLL_SECONDS)


def _requires_frigate_017_second_camera_restart(
    config: dict[str, Any],
    *,
    camera_exists: bool,
) -> bool:
    version = str(config.get("version") or "")
    cameras = config.get("cameras")
    return (
        re.match(r"^0\.17(?:[.-]|$)", version) is not None
        and isinstance(cameras, dict)
        and len(cameras) == 1
        and not camera_exists
    )


def full_sync_frigate(
    repository: Any,
    target: FrigateTarget,
    *,
    media_host: str = "127.0.0.1",
    client_factory: Callable[[FrigateTarget], FrigateClient] = FrigateClient,
) -> dict[str, int]:
    client = client_factory(target)
    try:
        client.capabilities()
        state = _full_sync_state(repository, client)
    except FrigateApiError as exc:
        raise exc.with_context(stage="inspect_configuration") from exc
    stale_cameras = state["stale_cameras"]
    stale_config_streams = state["stale_config_streams"]
    stale_streams = state["stale_streams"]

    for camera_key in stale_cameras:
        try:
            client.set_config(
                {"cameras": {camera_key: ""}},
                update_topic=f"config/cameras/{camera_key}/remove",
            )
        except FrigateApiError as exc:
            raise exc.with_context(stage="remove_camera", resource=camera_key) from exc

    # Frigate's own UI updates persistent go2rtc configuration before
    # changing the running go2rtc instance. This prevents an alias from being
    # recreated after restart and makes saved configuration authoritative.
    if stale_config_streams:
        try:
            client.set_config(
                {
                    "go2rtc": {
                        "streams": {
                            stream_key: "" for stream_key in stale_config_streams
                        }
                    }
                }
            )
            saved_config = client.raw_config()
        except FrigateApiError as exc:
            raise exc.with_context(stage="remove_stream_configuration") from exc
        configured_streams = saved_config.get("go2rtc", {}).get("streams", {})
        if not isinstance(configured_streams, dict):
            raise FrigateApiError(
                "invalid_response", stage="verify_stream_configuration"
            )
        remaining_config = [
            stream_key
            for stream_key in stale_config_streams
            if stream_key in configured_streams
        ]
        if remaining_config:
            raise FrigateApiError(
                "verification_failed",
                stage="verify_stream_configuration",
                resource=remaining_config[0],
            )

    try:
        runtime_streams = client.runtime_streams()
    except FrigateApiError as exc:
        raise exc.with_context(stage="inspect_runtime_streams") from exc
    delete_errors: dict[str, FrigateApiError] = {}
    for stream_key in stale_streams:
        if stream_key not in runtime_streams:
            continue
        try:
            client.delete_runtime_stream(stream_key)
        except FrigateApiError as exc:
            # go2rtc removes the live stream before patching its writable
            # primary YAML. Streams loaded from Frigate's generated secondary
            # config can therefore return HTTP 400 ("yaml: path not exist")
            # after the requested runtime deletion has already succeeded.
            # Preserve the response for diagnostics, but decide success from
            # the resulting runtime state below.
            delete_errors[stream_key] = exc

    try:
        remaining_runtime_streams = _wait_for_runtime_cleanup(client, stale_streams)
    except FrigateApiError as exc:
        if delete_errors:
            stream_key, delete_error = next(iter(delete_errors.items()))
            raise delete_error.with_context(
                stage="remove_runtime_stream", resource=stream_key
            ) from exc
        raise exc.with_context(stage="verify_runtime_cleanup") from exc
    if remaining_runtime_streams:
        stream_key = remaining_runtime_streams[0]
        if stream_key in delete_errors:
            raise delete_errors[stream_key].with_context(
                stage="remove_runtime_stream", resource=stream_key
            )
        raise FrigateApiError(
            "verification_failed",
            stage="verify_runtime_cleanup",
            resource=stream_key,
        )

    # Frigate 0.17.0 can save a camera removal while leaving the removed
    # capture workers and shared-memory frames alive. Restart only when the
    # stale workers remain after a short grace period for the documented
    # dynamic update.
    if stale_cameras:
        try:
            stale_workers = _wait_for_camera_worker_cleanup(
                client,
                stale_cameras,
                timeout=CAMERA_DYNAMIC_CLEANUP_GRACE_SECONDS,
            )
        except FrigateApiError as exc:
            raise exc.with_context(stage="inspect_camera_workers") from exc
        if stale_workers:
            try:
                client.restart()
            except FrigateApiError as exc:
                raise exc.with_context(stage="restart_frigate") from exc
            try:
                remaining_workers = _wait_for_camera_worker_cleanup(
                    client, stale_workers
                )
            except FrigateApiError as exc:
                raise exc.with_context(stage="wait_for_frigate_restart") from exc
            if remaining_workers:
                raise FrigateApiError(
                    "verification_failed",
                    stage="verify_camera_worker_cleanup",
                    resource=remaining_workers[0],
                )

    try:
        verified = _full_sync_state(repository, client)
    except FrigateApiError as exc:
        raise exc.with_context(stage="verify_cleanup") from exc
    remaining = [*verified["stale_cameras"], *verified["stale_streams"]]
    if remaining:
        raise FrigateApiError(
            "verification_failed",
            stage="verify_cleanup",
            resource=remaining[0],
        )
    try:
        reconciliation = reconcile_frigate(
            repository,
            target,
            media_host=media_host,
            client_factory=lambda _target: client,
        )
    except FrigateApiError as exc:
        raise exc.with_context(stage="reconcile_current_cameras") from exc
    return {
        "removed_cameras": len(stale_cameras),
        "removed_streams": len(stale_streams),
        **reconciliation,
    }


def remove_frigate_camera(
    repository: Any,
    target: FrigateTarget,
    camera_uuid: str,
    *,
    client_factory: Callable[[FrigateTarget], FrigateClient] = FrigateClient,
) -> dict[str, int]:
    binding = repository.frigate_binding(target.target_id, camera_uuid)
    if binding is None:
        repository.deselect_frigate_camera(target.target_id, camera_uuid)
        return {"removed_cameras": 0, "removed_streams": 0}
    camera_key = str(binding["frigate_camera_key"])
    if not camera_key.startswith(KEY_PREFIX):
        raise FrigateApiError("ownership_verification_failed")
    stream_keys = [f"{camera_key}_record", f"{camera_key}_detect"]
    client = client_factory(target)
    try:
        client.capabilities()
        saved_config = client.raw_config()
        runtime_streams = client.runtime_streams()
    except FrigateApiError as exc:
        raise exc.with_context(stage="inspect_configuration") from exc

    configured_cameras = saved_config.get("cameras", {})
    configured_streams = saved_config.get("go2rtc", {}).get("streams", {})
    if not isinstance(configured_cameras, dict) or not isinstance(configured_streams, dict):
        raise FrigateApiError("invalid_response", stage="inspect_configuration")
    camera_exists = camera_key in configured_cameras
    configured_aliases = [name for name in stream_keys if name in configured_streams]
    runtime_aliases = [name for name in stream_keys if name in runtime_streams]

    if camera_exists:
        try:
            client.set_config(
                {"cameras": {camera_key: ""}},
                update_topic=f"config/cameras/{camera_key}/remove",
            )
        except FrigateApiError as exc:
            raise exc.with_context(stage="remove_camera", resource=camera_key) from exc
    if configured_aliases:
        try:
            client.set_config(
                {"go2rtc": {"streams": {name: "" for name in configured_aliases}}}
            )
        except FrigateApiError as exc:
            raise exc.with_context(stage="remove_stream_configuration") from exc

    delete_errors: dict[str, FrigateApiError] = {}
    for stream_key in runtime_aliases:
        try:
            client.delete_runtime_stream(stream_key)
        except FrigateApiError as exc:
            delete_errors[stream_key] = exc
    try:
        remaining_runtime = _wait_for_runtime_cleanup(client, runtime_aliases)
    except FrigateApiError as exc:
        raise exc.with_context(stage="verify_runtime_cleanup") from exc
    if remaining_runtime:
        stream_key = remaining_runtime[0]
        if stream_key in delete_errors:
            raise delete_errors[stream_key].with_context(
                stage="remove_runtime_stream", resource=stream_key
            )
        raise FrigateApiError(
            "verification_failed",
            stage="verify_runtime_cleanup",
            resource=stream_key,
        )

    if camera_exists:
        try:
            stale_workers = _wait_for_camera_worker_cleanup(
                client, [camera_key], timeout=CAMERA_DYNAMIC_CLEANUP_GRACE_SECONDS
            )
            if stale_workers:
                client.restart()
                stale_workers = _wait_for_camera_worker_cleanup(client, stale_workers)
        except FrigateApiError as exc:
            raise exc.with_context(stage="verify_camera_worker_cleanup", resource=camera_key) from exc
        if stale_workers:
            raise FrigateApiError(
                "verification_failed",
                stage="verify_camera_worker_cleanup",
                resource=camera_key,
            )

    try:
        verified = client.raw_config()
        verified_runtime = client.runtime_streams()
    except FrigateApiError as exc:
        raise exc.with_context(stage="verify_cleanup") from exc
    if camera_key in verified.get("cameras", {}):
        raise FrigateApiError("verification_failed", stage="verify_cleanup", resource=camera_key)
    verified_streams = verified.get("go2rtc", {}).get("streams", {})
    remaining = [
        name for name in stream_keys if name in verified_streams or name in verified_runtime
    ]
    if remaining:
        raise FrigateApiError("verification_failed", stage="verify_cleanup", resource=remaining[0])

    repository.deselect_frigate_camera(target.target_id, camera_uuid)
    repository.remove_frigate_binding(target.target_id, camera_uuid)
    return {
        "removed_cameras": int(camera_exists),
        "removed_streams": len(set(configured_aliases) | set(runtime_aliases)),
    }


def _stream_for_role(camera: dict[str, Any], role: str) -> dict[str, Any] | None:
    usable = [
        stream
        for stream in camera.get("streams", [])
        if role in stream.get("roles", [])
        and stream.get("health_status") not in {"offline", "auth_failed"}
    ]
    return usable[0] if usable else None


def _video_metadata(stream: dict[str, Any]) -> tuple[int, int, int]:
    width = int(stream.get("probed_width") or stream.get("width") or 0)
    height = int(stream.get("probed_height") or stream.get("height") or 0)
    fps = max(1, int(round(float(stream.get("probed_fps") or stream.get("fps") or 0))))
    if width <= 0 or height <= 0:
        raise FrigateApiError("detect_metadata_unavailable")
    return width, height, fps


def media_host_from_inventory(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_RESPONSE_BYTES:
            raise FrigateApiError("media_host_unavailable")
        inventory = json.loads(path.read_text(encoding="utf-8"))
        value = inventory.get("network", {}).get("address")
        address = ipaddress.ip_address(value)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise FrigateApiError("media_host_unavailable") from exc
    if address.is_unspecified or address.is_multicast or not (address.is_private or address.is_loopback):
        raise FrigateApiError("media_host_unavailable")
    return str(address)


def desired_camera(
    camera: dict[str, Any],
    password: str,
    media_host: str = "127.0.0.1",
) -> dict[str, Any] | None:
    record = _stream_for_role(camera, "record")
    detect = _stream_for_role(camera, "detect")
    if record is None or detect is None:
        return None
    camera_uuid = str(camera["camera_uuid"])
    key = frigate_camera_key(camera_uuid)
    record_alias = f"{key}_record"
    detect_alias = f"{key}_detect"
    width, height, fps = _video_metadata(detect)
    parsed_media_host = ipaddress.ip_address(media_host)
    source_host = f"[{parsed_media_host}]" if parsed_media_host.version == 6 else str(parsed_media_host)

    def source(stream: dict[str, Any]) -> str:
        credentials = urllib.parse.quote(password, safe="")
        return (
            f"rtsp://{CAMADMIRAL_RTSP_USERNAME}:{credentials}@{source_host}:"
            f"{CAMADMIRAL_RTSP_PORT}/{stream['stream_key']}"
        )

    ffmpeg_inputs = [
        {
            "path": f"rtsp://127.0.0.1:{FRIGATE_GO2RTC_PORT}/{record_alias}",
            "input_args": "preset-rtsp-restream",
            "roles": ["record"],
        }
    ]
    if record["stream_uuid"] == detect["stream_uuid"]:
        ffmpeg_inputs[0]["roles"].append("detect")
    else:
        ffmpeg_inputs.append(
            {
                "path": f"rtsp://127.0.0.1:{FRIGATE_GO2RTC_PORT}/{detect_alias}",
                "input_args": "preset-rtsp-restream",
                "roles": ["detect"],
            }
        )
    camera_config = {
        "enabled": True,
        "friendly_name": str(camera["display_name"]),
        "ffmpeg": {"inputs": ffmpeg_inputs},
        "detect": {"width": width, "height": height, "fps": fps},
        "live": {"streams": {"Record": record_alias, "Detect": detect_alias}},
    }
    streams = {record_alias: [source(record)], detect_alias: [source(detect)]}
    secret_free = {
        "camera_uuid": camera_uuid,
        "key": key,
        "name": camera["display_name"],
        "record_stream_uuid": record["stream_uuid"],
        "detect_stream_uuid": detect["stream_uuid"],
        "media_host": str(parsed_media_host),
        "camera_config": camera_config,
        "stream_keys": sorted(streams),
    }
    desired_hash = hashlib.sha256(
        json.dumps(secret_free, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **secret_free,
        "desired_hash": desired_hash,
        "camera_config": camera_config,
        "streams": streams,
    }


def frigate_camera_configuration(
    repository: Any,
    target: FrigateTarget,
    camera_uuid: str,
    *,
    media_host: str = "127.0.0.1",
) -> dict[str, str]:
    camera = next(
        (
            item
            for item in repository.consumer_inventory()
            if str(item["camera_uuid"]) == camera_uuid
        ),
        None,
    )
    if camera is None:
        raise FrigateApiError("camera_not_found")
    password = repository.rtsp_access_password()
    desired = desired_camera(camera, password, media_host)
    if desired is None:
        raise FrigateApiError("streams_unavailable")
    payload = {
        "cameras": {desired["key"]: desired["camera_config"]},
        "go2rtc": {"streams": desired["streams"]},
    }
    configuration = yaml.safe_dump(payload, sort_keys=False).strip()
    encoded_password = urllib.parse.quote(password, safe="")
    return {
        "configuration": configuration,
        "display_configuration": configuration.replace(encoded_password, "********"),
    }


def _owned_camera_matches(actual: object, desired: dict[str, Any], raw_paths: dict[str, Any]) -> bool:
    if not isinstance(actual, dict):
        return False
    expected = desired["camera_config"]
    if actual.get("friendly_name") != expected["friendly_name"]:
        return False
    actual_detect = actual.get("detect")
    if not isinstance(actual_detect, dict) or any(
        int(actual_detect.get(field) or 0) != value
        for field, value in expected["detect"].items()
    ):
        return False
    actual_live = actual.get("live")
    if not isinstance(actual_live, dict) or actual_live.get("streams") != expected["live"]["streams"]:
        return False
    raw_camera = raw_paths.get("cameras", {}).get(desired["key"], {})
    raw_inputs = raw_camera.get("ffmpeg", {}).get("inputs") if isinstance(raw_camera, dict) else None
    expected_inputs = [
        {"path": item["path"], "roles": item["roles"]}
        for item in expected["ffmpeg"]["inputs"]
    ]
    return raw_inputs == expected_inputs


def _actual_matches(
    desired: dict[str, Any],
    config: dict[str, Any],
    raw_paths: dict[str, Any],
    runtime_streams: dict[str, Any],
) -> bool:
    camera = config.get("cameras", {}).get(desired["key"])
    if not _owned_camera_matches(camera, desired, raw_paths):
        return False
    configured_streams = raw_paths.get("go2rtc", {}).get("streams", {})
    return all(
        configured_streams.get(name) == sources and name in runtime_streams
        for name, sources in desired["streams"].items()
    )


def reconcile_frigate(
    repository: Any,
    target: FrigateTarget,
    *,
    media_host: str = "127.0.0.1",
    client_factory: Callable[[FrigateTarget], FrigateClient] = FrigateClient,
) -> dict[str, int]:
    cameras = selected_camera_inventory(repository, target.target_id)
    if not cameras:
        return {"applied": 0, "pending": 0}
    client = client_factory(target)
    client.capabilities()
    config = client.config()
    raw_paths = client.raw_paths()
    runtime_streams = client.runtime_streams()
    stats = client.stats()
    password = repository.rtsp_access_password()
    applied = 0
    pending = 0
    for camera in cameras:
        binding = repository.frigate_binding(target.target_id, camera["camera_uuid"])
        if not camera.get("enabled", True):
            if binding is None:
                applied += 1
                continue
            key = str(binding["frigate_camera_key"])
            actual_camera = config.get("cameras", {}).get(key)
            if not isinstance(actual_camera, dict):
                applied += 1
                continue
            if actual_camera.get("enabled") is False:
                applied += 1
                continue
            repository.set_frigate_camera_enabled_applied(
                target.target_id,
                camera["camera_uuid"],
                False,
            )
            repository.record_frigate_attempt(
                target.target_id,
                camera["camera_uuid"],
                key,
                binding["record_stream_uuid"],
                binding["detect_stream_uuid"],
                binding["desired_hash"],
            )
            try:
                client.set_config(
                    {"cameras": {key: {"enabled": False}}},
                    update_topic=f"config/cameras/{key}/enabled",
                )
                verified_config = client.config()
                verified_camera = verified_config.get("cameras", {}).get(key)
                if not isinstance(verified_camera, dict) or verified_camera.get("enabled") is not False:
                    raise FrigateApiError("verification_failed")
            except FrigateApiError as exc:
                repository.complete_frigate_attempt(
                    target.target_id,
                    camera["camera_uuid"],
                    status="error",
                    error_code=exc.code,
                )
                pending += 1
                continue
            repository.complete_frigate_attempt(
                target.target_id,
                camera["camera_uuid"],
                status="applied",
                applied_hash=binding.get("applied_hash") or binding["desired_hash"],
            )
            config = verified_config
            applied += 1
            continue
        desired = desired_camera(camera, password, media_host)
        if desired is None:
            pending += 1
            continue
        camera_exists = desired["key"] in config.get("cameras", {})
        camera_running = desired["key"] in stats
        configured_streams = raw_paths.get("go2rtc", {}).get("streams", {})
        aliases_exist = any(alias in configured_streams for alias in desired["streams"])
        if binding is None and (camera_exists or aliases_exist):
            pending += 1
            continue
        actual_matches = _actual_matches(desired, config, raw_paths, runtime_streams)
        if (
            binding is not None
            and bool(binding.get("camera_enabled_applied", 1))
            and binding.get("applied_hash") == desired["desired_hash"]
            and actual_matches
            and camera_running
        ):
            if binding.get("status") != "applied":
                repository.complete_frigate_attempt(
                    target.target_id,
                    camera["camera_uuid"],
                    status="applied",
                    applied_hash=desired["desired_hash"],
                )
            applied += 1
            continue
        if camera_exists and actual_matches and not camera_running:
            # Frigate 0.17 camera add events are not idempotent. Re-publishing
            # an add for an existing camera starts another set of workers and
            # leaves the previous processes alive. Keep the binding pending and
            # wait for Frigate to report the process instead.
            if binding is not None and (
                binding.get("status") != "error"
                or binding.get("last_error_code") != "camera_start_pending"
            ):
                repository.record_frigate_attempt(
                    target.target_id,
                    camera["camera_uuid"],
                    desired["key"],
                    desired["record_stream_uuid"],
                    desired["detect_stream_uuid"],
                    desired["desired_hash"],
                )
                repository.complete_frigate_attempt(
                    target.target_id,
                    camera["camera_uuid"],
                    status="error",
                    error_code="camera_start_pending",
                )
            pending += 1
            continue
        repository.record_frigate_attempt(
            target.target_id,
            camera["camera_uuid"],
            desired["key"],
            desired["record_stream_uuid"],
            desired["detect_stream_uuid"],
            desired["desired_hash"],
        )
        try:
            restore_camadmiral_enabled = (
                camera_exists
                and binding is not None
                and not bool(binding.get("camera_enabled_applied", 1))
            )
            camera_update = desired["camera_config"]
            if camera_exists:
                camera_update = {
                    field: value
                    for field, value in camera_update.items()
                    if field != "enabled"
                }
            restart_for_second_camera = _requires_frigate_017_second_camera_restart(
                config,
                camera_exists=camera_exists,
            )
            client.set_config(
                {"cameras": {desired["key"]: camera_update}},
                update_topic=(
                    f"config/cameras/{desired['key']}/add"
                    if not camera_exists
                    else None
                ),
            )
            if restore_camadmiral_enabled:
                client.set_config(
                    {"cameras": {desired["key"]: {"enabled": True}}},
                    update_topic=f"config/cameras/{desired['key']}/enabled",
                )
            client.set_config({"go2rtc": {"streams": desired["streams"]}})
            for alias, sources in desired["streams"].items():
                client.set_runtime_stream(alias, sources[0])
            if restart_for_second_camera:
                client.restart()
                time.sleep(FRIGATE_RESTART_SETTLE_SECONDS)
                missing_workers = _wait_for_camera_workers(client, [desired["key"]])
                if missing_workers:
                    raise FrigateApiError("camera_start_pending")
            verified_config = client.config()
            verified_raw_paths = client.raw_paths()
            verified_runtime = client.runtime_streams()
            verified_stats = client.stats()
            if not _actual_matches(desired, verified_config, verified_raw_paths, verified_runtime):
                raise FrigateApiError("verification_failed")
            if desired["key"] not in verified_stats:
                raise FrigateApiError("camera_start_pending")
        except FrigateApiError as exc:
            repository.complete_frigate_attempt(
                target.target_id,
                camera["camera_uuid"],
                status="error",
                error_code=exc.code,
            )
            pending += 1
            continue
        repository.complete_frigate_attempt(
            target.target_id,
            camera["camera_uuid"],
            status="applied",
            applied_hash=desired["desired_hash"],
        )
        repository.set_frigate_camera_enabled_applied(
            target.target_id,
            camera["camera_uuid"],
            True,
        )
        config, raw_paths, runtime_streams, stats = (
            verified_config,
            verified_raw_paths,
            verified_runtime,
            verified_stats,
        )
        applied += 1
    return {"applied": applied, "pending": pending}
