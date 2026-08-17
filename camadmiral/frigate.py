from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import settings

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
CAMADMIRAL_RTSP_USERNAME = "camadmiral"
CAMADMIRAL_RTSP_PORT = 18554
FRIGATE_GO2RTC_PORT = 8554
KEY_PREFIX = "camadmiral_"
REQUIRED_CAPABILITIES = {
    "/config": "get",
    "/config/raw_paths": "get",
    "/config/set": "put",
    "/go2rtc/streams": "get",
    "/go2rtc/streams/{stream_name}": "put",
}


class FrigateApiError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FrigateTarget:
    target_id: str
    name: str
    api_url: str


def load_frigate_targets() -> list[FrigateTarget]:
    return [
        FrigateTarget(target.target_id, target.name, target.api_url)
        for target in settings().integrations.frigate.targets
        if target.sync_cameras
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
            if exc.code in {401, 403}:
                raise FrigateApiError("authorization_required") from exc
            if exc.code == 404:
                raise FrigateApiError("capability_unavailable") from exc
            raise FrigateApiError("request_rejected") from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise FrigateApiError("target_unavailable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FrigateApiError("response_too_large")
        try:
            return json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise FrigateApiError("invalid_response") from exc

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

    def runtime_streams(self) -> dict[str, Any]:
        result = self._request("GET", "/api/go2rtc/streams")
        if not isinstance(result, dict):
            raise FrigateApiError("invalid_response")
        return result

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


def frigate_camera_key(camera_uuid: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]", "_", camera_uuid)
    return f"{KEY_PREFIX}{normalized}"


def _stream_for_role(camera: dict[str, Any], role: str) -> dict[str, Any] | None:
    usable = [
        stream
        for stream in camera.get("streams", [])
        if role in stream.get("roles", []) and stream.get("health_status") == "healthy"
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
    client = client_factory(target)
    client.capabilities()
    config = client.config()
    raw_paths = client.raw_paths()
    runtime_streams = client.runtime_streams()
    password = repository.rtsp_access_password()
    applied = 0
    pending = 0
    for camera in repository.consumer_inventory():
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
        configured_streams = raw_paths.get("go2rtc", {}).get("streams", {})
        aliases_exist = any(alias in configured_streams for alias in desired["streams"])
        if binding is None and (camera_exists or aliases_exist):
            pending += 1
            continue
        if (
            binding is not None
            and bool(binding.get("camera_enabled_applied", 1))
            and binding.get("status") == "applied"
            and binding.get("applied_hash") == desired["desired_hash"]
            and _actual_matches(desired, config, raw_paths, runtime_streams)
        ):
            applied += 1
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
            client.set_config(
                {"cameras": {desired["key"]: camera_update}},
                update_topic=f"config/cameras/{desired['key']}/add" if not camera_exists else None,
            )
            if restore_camadmiral_enabled:
                client.set_config(
                    {"cameras": {desired["key"]: {"enabled": True}}},
                    update_topic=f"config/cameras/{desired['key']}/enabled",
                )
            client.set_config({"go2rtc": {"streams": desired["streams"]}})
            for alias, sources in desired["streams"].items():
                client.set_runtime_stream(alias, sources[0])
            verified_config = client.config()
            verified_raw_paths = client.raw_paths()
            verified_runtime = client.runtime_streams()
            if not _actual_matches(desired, verified_config, verified_raw_paths, verified_runtime):
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
            applied_hash=desired["desired_hash"],
        )
        repository.set_frigate_camera_enabled_applied(
            target.target_id,
            camera["camera_uuid"],
            True,
        )
        config, raw_paths, runtime_streams = verified_config, verified_raw_paths, verified_runtime
        applied += 1
    return {"applied": applied, "pending": pending}
