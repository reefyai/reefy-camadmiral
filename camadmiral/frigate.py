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
from yaml.nodes import MappingNode, ScalarNode

from .discovery import default_lan_interface

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
FRIGATE_VERIFY_TIMEOUT_SECONDS = 30.0
FRIGATE_VERIFY_POLL_SECONDS = 0.5
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
FRIGATE_ADDRESS_MODES = {"lan", "localhost"}


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


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class FrigateTarget:
    target_id: str
    name: str
    api_url: str
    address_mode: str | None = None


def normalize_frigate_api_url(value: object) -> str:
    if not isinstance(value, str):
        raise FrigateApiError("invalid_target_url")
    raw = value.strip()
    if not raw or any(character.isspace() or ord(character) < 32 for character in raw):
        raise FrigateApiError("invalid_target_url")
    try:
        parsed = urllib.parse.urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise FrigateApiError("invalid_target_url") from exc
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise FrigateApiError("invalid_target_url")
    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = f"{host}:{port}" if port is not None else host
    path = parsed.path.rstrip("/")
    return f"{scheme}://{authority}{path}"


def load_frigate_targets(repository: Any) -> list[FrigateTarget]:
    return [
        FrigateTarget(
            str(target["target_id"]),
            str(target["name"]),
            str(target["api_url"]),
            target.get("address_mode"),
        )
        for target in repository.frigate_targets()
    ]


class FrigateClient:
    def __init__(self, target: FrigateTarget, timeout: float = 5.0):
        self.target = target
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        raw_body: bytes | None = None,
    ) -> Any:
        if payload is not None and raw_body is not None:
            raise ValueError("payload and raw_body are mutually exclusive")
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            body = raw_body
            headers["Content-Type"] = "text/plain"
        request = urllib.request.Request(
            f"{self.target.api_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
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
        result = self.raw_config_text()
        try:
            parsed = yaml.safe_load(result)
        except yaml.YAMLError as exc:
            raise FrigateApiError("invalid_response") from exc
        if not isinstance(parsed, dict):
            raise FrigateApiError("invalid_response")
        return parsed

    def raw_config_text(self) -> str:
        result = self._request("GET", "/api/config/raw")
        if not isinstance(result, str):
            raise FrigateApiError("invalid_response")
        return result

    def save_raw_config(self, raw_config: str) -> None:
        result = self._request(
            "POST",
            "/api/config/save?save_option=save",
            raw_body=raw_config.encode("utf-8"),
        )
        if not isinstance(result, dict) or result.get("success") is not True:
            raise FrigateApiError("configuration_rejected")

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

    def set_config(
        self,
        config_data: dict[str, Any],
        *,
        update_topic: str | None = None,
        requires_restart: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "requires_restart": int(requires_restart),
            "config_data": config_data,
        }
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


def _empty_top_level_mapping(raw_config: str, mapping_key: str) -> str:
    """Replace a top-level YAML mapping with an explicit empty mapping.

    Frigate 0.17 can emit an invalid standalone ``{}`` when its incremental
    updater removes the final child from a mapping whose key has an inline
    comment. Raw-save the minimal text edit instead so comments and unrelated
    operator configuration remain untouched.
    """
    try:
        document = yaml.compose(raw_config)
    except yaml.YAMLError as exc:
        raise FrigateApiError("invalid_response") from exc
    if not isinstance(document, MappingNode):
        raise FrigateApiError("invalid_response")

    mapping_node: MappingNode | None = None
    key_node: ScalarNode | None = None
    for candidate_key, candidate_value in document.value:
        if (
            isinstance(candidate_key, ScalarNode)
            and candidate_key.value == mapping_key
        ):
            key_node = candidate_key
            if isinstance(candidate_value, MappingNode):
                mapping_node = candidate_value
            break
    if key_node is None or mapping_node is None:
        raise FrigateApiError("invalid_response")

    line_start = key_node.start_mark.index - key_node.start_mark.column
    line_end = raw_config.find("\n", key_node.end_mark.index)
    if line_end < 0:
        line_end = len(raw_config)
        newline = ""
    else:
        newline = "\n"
    colon = raw_config.find(":", key_node.end_mark.index, line_end)
    if colon < 0:
        raise FrigateApiError("invalid_response")

    comment = raw_config.find("#", colon + 1, line_end)
    new_key_line = raw_config[line_start : colon + 1] + " {}"
    if comment >= 0:
        new_key_line += " " + raw_config[comment:line_end].strip()
    new_key_line += newline

    transformed = (
        raw_config[:line_start]
        + new_key_line
        + raw_config[mapping_node.end_mark.index :]
    )
    try:
        parsed = yaml.safe_load(transformed)
    except yaml.YAMLError as exc:
        raise FrigateApiError("invalid_response") from exc
    if not isinstance(parsed, dict) or parsed.get(mapping_key) != {}:
        raise FrigateApiError("invalid_response")
    return transformed


def _remove_saved_camera(
    client: FrigateClient,
    camera_key: str,
    *,
    final_camera: bool,
) -> None:
    if final_camera:
        raw_config = client.raw_config_text()
        client.save_raw_config(_empty_top_level_mapping(raw_config, "cameras"))
        return
    client.set_config(
        {"cameras": {camera_key: ""}},
        requires_restart=True,
    )


def selected_camera_inventory(repository: Any, target_id: str) -> list[dict[str, Any]]:
    target = repository.frigate_target(target_id)
    target_address_mode = target.get("address_mode") if target is not None else None
    selections = {
        str(selection["camera_uuid"]): str(selection["address_mode"])
        for selection in repository.frigate_camera_selections(target_id)
    }
    if not selections:
        return []
    return [
        {
            **camera,
            "frigate_address_mode": (
                str(target_address_mode)
                if target_address_mode is not None
                else selections[str(camera["camera_uuid"])]
            ),
        }
        for camera in repository.consumer_inventory()
        if str(camera["camera_uuid"]) in selections
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
        "configured_camera_count": len(configured_cameras),
        "desired_cameras": desired_cameras,
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


def frigate_restart_required(
    repository: Any,
    target: FrigateTarget,
    *,
    client_factory: Callable[[FrigateTarget], FrigateClient] = FrigateClient,
) -> bool:
    """Return whether removed CamAdmiral resources are still running."""
    client = client_factory(target)
    client.capabilities()
    live_config = client.config()
    state = _full_sync_state(repository, client)
    desired_cameras = state["desired_cameras"]
    live_cameras = live_config.get("cameras", {})
    if not isinstance(live_cameras, dict):
        raise FrigateApiError("invalid_response")
    stale_live_cameras = {
        camera_key
        for camera_key in live_cameras
        if camera_key.startswith(KEY_PREFIX) and camera_key not in desired_cameras
    }
    worker_stats = client.stats()
    stale_workers = {
        camera_key
        for camera_key in worker_stats
        if camera_key.startswith(KEY_PREFIX) and camera_key not in desired_cameras
    }
    return bool(
        stale_live_cameras or stale_workers or state["stale_runtime_streams"]
    )


def full_sync_frigate(
    repository: Any,
    target: FrigateTarget,
    *,
    media_host: str = "127.0.0.1",
    media_host_resolver: Callable[[str], str] | None = None,
    client_factory: Callable[[FrigateTarget], FrigateClient] = FrigateClient,
) -> dict[str, int | bool]:
    client = client_factory(target)
    try:
        client.capabilities()
        state = _full_sync_state(repository, client)
        worker_stats = client.stats()
    except FrigateApiError as exc:
        raise exc.with_context(stage="inspect_configuration") from exc
    stale_cameras = state["stale_cameras"]
    desired_cameras = state["desired_cameras"]
    stale_config_streams = state["stale_config_streams"]
    stale_streams = state["stale_streams"]
    stale_workers = {
        camera_key
        for camera_key in worker_stats
        if camera_key.startswith(KEY_PREFIX) and camera_key not in desired_cameras
    }
    restart_recommended = bool(stale_cameras or stale_workers)

    final_stale_camera_empties_section = (
        bool(stale_cameras)
        and state["configured_camera_count"] == len(stale_cameras)
    )
    for index, camera_key in enumerate(stale_cameras):
        try:
            # Frigate 0.17 shares one mutable camera configuration across
            # several dynamic-update subscribers. Hot removal lets the first
            # subscriber remove the camera and crashes the others with
            # KeyError. Camera removal is therefore always persisted for the
            # next operator-controlled restart instead of attempted live.
            _remove_saved_camera(
                client,
                camera_key,
                final_camera=(
                    final_stale_camera_empties_section
                    and index == len(stale_cameras) - 1
                ),
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
                },
                requires_restart=restart_recommended,
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

    if not restart_recommended:
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

    try:
        verified = _full_sync_state(repository, client)
    except FrigateApiError as exc:
        raise exc.with_context(stage="verify_cleanup") from exc
    remaining = [*verified["stale_cameras"], *verified["stale_config_streams"]]
    if not restart_recommended:
        remaining.extend(verified["stale_runtime_streams"])
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
            media_host_resolver=media_host_resolver,
            client_factory=lambda _target: client,
            allow_restart=not restart_recommended,
        )
    except FrigateApiError as exc:
        raise exc.with_context(stage="reconcile_current_cameras") from exc
    return {
        "removed_cameras": len(stale_cameras),
        "removed_streams": len(stale_streams),
        "restart_recommended": restart_recommended,
        **reconciliation,
    }


def remove_frigate_camera(
    repository: Any,
    target: FrigateTarget,
    camera_uuid: str,
    *,
    client_factory: Callable[[FrigateTarget], FrigateClient] = FrigateClient,
) -> dict[str, int | bool]:
    binding = repository.frigate_binding(target.target_id, camera_uuid)
    if binding is None:
        repository.deselect_frigate_camera(target.target_id, camera_uuid)
        return {
            "removed_cameras": 0,
            "removed_streams": 0,
            "restart_recommended": False,
        }
    camera_key = str(binding["frigate_camera_key"])
    if not camera_key.startswith(KEY_PREFIX):
        raise FrigateApiError("ownership_verification_failed")
    stream_keys = [f"{camera_key}_record", f"{camera_key}_detect"]
    client = client_factory(target)
    try:
        client.capabilities()
        saved_config = client.raw_config()
        runtime_streams = client.runtime_streams()
        worker_stats = client.stats()
    except FrigateApiError as exc:
        raise exc.with_context(stage="inspect_configuration") from exc

    configured_cameras = saved_config.get("cameras", {})
    configured_streams = saved_config.get("go2rtc", {}).get("streams", {})
    if not isinstance(configured_cameras, dict) or not isinstance(configured_streams, dict):
        raise FrigateApiError("invalid_response", stage="inspect_configuration")
    camera_exists = camera_key in configured_cameras
    configured_aliases = [name for name in stream_keys if name in configured_streams]
    runtime_aliases = [name for name in stream_keys if name in runtime_streams]
    restart_recommended = camera_exists or camera_key in worker_stats

    if camera_exists:
        try:
            _remove_saved_camera(
                client,
                camera_key,
                final_camera=len(configured_cameras) == 1,
            )
        except FrigateApiError as exc:
            raise exc.with_context(stage="remove_camera", resource=camera_key) from exc
    if configured_aliases:
        try:
            client.set_config(
                {"go2rtc": {"streams": {name: "" for name in configured_aliases}}},
                requires_restart=restart_recommended,
            )
        except FrigateApiError as exc:
            raise exc.with_context(stage="remove_stream_configuration") from exc

    if not restart_recommended:
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

    try:
        verified = client.raw_config()
        verified_runtime = client.runtime_streams()
    except FrigateApiError as exc:
        raise exc.with_context(stage="verify_cleanup") from exc
    if camera_key in verified.get("cameras", {}):
        raise FrigateApiError("verification_failed", stage="verify_cleanup", resource=camera_key)
    verified_streams = verified.get("go2rtc", {}).get("streams", {})
    remaining = [name for name in stream_keys if name in verified_streams]
    if not restart_recommended:
        remaining.extend(name for name in stream_keys if name in verified_runtime)
    if remaining:
        raise FrigateApiError("verification_failed", stage="verify_cleanup", resource=remaining[0])

    repository.deselect_frigate_camera(target.target_id, camera_uuid)
    repository.remove_frigate_binding(target.target_id, camera_uuid)
    return {
        "removed_cameras": int(camera_exists),
        "removed_streams": len(set(configured_aliases) | set(runtime_aliases)),
        "restart_recommended": restart_recommended,
    }


def _stream_for_role(camera: dict[str, Any], role: str) -> dict[str, Any] | None:
    usable = [
        stream
        for stream in camera.get("streams", [])
        if role in stream.get("roles", [])
        and stream.get("health_status") not in {"offline", "auth_failed"}
    ]
    return usable[0] if usable else None


def _video_dimensions(stream: dict[str, Any]) -> tuple[int, int]:
    width = int(stream.get("probed_width") or stream.get("width") or 0)
    height = int(stream.get("probed_height") or stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise FrigateApiError("detect_metadata_unavailable")
    return width, height


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


def media_host_for_mode(path: Path, address_mode: str) -> str:
    if address_mode not in FRIGATE_ADDRESS_MODES:
        raise FrigateApiError("invalid_address_mode")
    if address_mode == "localhost":
        return "localhost"
    try:
        return str(default_lan_interface().address)
    except (OSError, RuntimeError, ValueError):
        return media_host_from_inventory(path)


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
    width, height = _video_dimensions(detect)
    try:
        parsed_media_host = ipaddress.ip_address(media_host)
    except ValueError:
        if len(media_host) > 253 or not all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in media_host.rstrip(".").split(".")
        ):
            raise FrigateApiError("media_host_unavailable")
        source_host = media_host.rstrip(".")
    else:
        source_host = (
            f"[{parsed_media_host}]" if parsed_media_host.version == 6 else str(parsed_media_host)
        )

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
        "detect": {"width": width, "height": height},
        "live": {"streams": {"Record": record_alias, "Detect": detect_alias}},
    }
    streams = {record_alias: [source(record)], detect_alias: [source(detect)]}
    secret_free = {
        "camera_uuid": camera_uuid,
        "key": key,
        "name": camera["display_name"],
        "record_stream_uuid": record["stream_uuid"],
        "detect_stream_uuid": detect["stream_uuid"],
        "media_host": media_host,
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


def _owned_camera_mismatch(
    actual: object,
    desired: dict[str, Any],
    raw_paths: dict[str, Any],
    raw_config: dict[str, Any],
) -> str | None:
    if not isinstance(actual, dict):
        return "camera_configuration_missing"
    expected = desired["camera_config"]
    if actual.get("friendly_name") != expected["friendly_name"]:
        return "camera_name_mismatch"
    actual_detect = actual.get("detect")
    try:
        detect_matches = isinstance(actual_detect, dict) and all(
            int(actual_detect.get(field) or 0) == value
            for field, value in expected["detect"].items()
        )
    except (TypeError, ValueError):
        detect_matches = False
    if not detect_matches:
        return "detect_settings_mismatch"
    actual_live = actual.get("live")
    if (
        not isinstance(actual_live, dict)
        or actual_live.get("streams") != expected["live"]["streams"]
    ):
        return "live_streams_mismatch"
    raw_cameras = raw_paths.get("cameras", {})
    raw_camera = (
        raw_cameras.get(desired["key"], {}) if isinstance(raw_cameras, dict) else {}
    )
    raw_inputs = raw_camera.get("ffmpeg", {}).get("inputs") if isinstance(raw_camera, dict) else None
    expected_inputs = [
        {"path": item["path"], "roles": item["roles"]}
        for item in expected["ffmpeg"]["inputs"]
    ]
    if raw_inputs != expected_inputs:
        return "ffmpeg_inputs_mismatch"
    saved_cameras = raw_config.get("cameras", {})
    saved_camera = (
        saved_cameras.get(desired["key"], {}) if isinstance(saved_cameras, dict) else {}
    )
    saved_detect = saved_camera.get("detect") if isinstance(saved_camera, dict) else None
    if isinstance(saved_detect, dict) and "fps" in saved_detect:
        return "detect_settings_mismatch"
    return None


def _owned_camera_matches(
    actual: object,
    desired: dict[str, Any],
    raw_paths: dict[str, Any],
    raw_config: dict[str, Any],
) -> bool:
    return _owned_camera_mismatch(actual, desired, raw_paths, raw_config) is None


def _actual_mismatch(
    desired: dict[str, Any],
    config: dict[str, Any],
    raw_paths: dict[str, Any],
    raw_config: dict[str, Any],
    runtime_streams: dict[str, Any],
) -> str | None:
    cameras = config.get("cameras", {})
    camera = cameras.get(desired["key"]) if isinstance(cameras, dict) else None
    camera_mismatch = _owned_camera_mismatch(camera, desired, raw_paths, raw_config)
    if camera_mismatch is not None:
        return camera_mismatch
    go2rtc = raw_paths.get("go2rtc", {})
    configured_streams = go2rtc.get("streams", {}) if isinstance(go2rtc, dict) else {}
    if not isinstance(configured_streams, dict) or any(
        configured_streams.get(name) != sources
        for name, sources in desired["streams"].items()
    ):
        return "saved_stream_mismatch"
    if any(name not in runtime_streams for name in desired["streams"]):
        return "runtime_stream_missing"
    return None


def _actual_matches(
    desired: dict[str, Any],
    config: dict[str, Any],
    raw_paths: dict[str, Any],
    raw_config: dict[str, Any],
    runtime_streams: dict[str, Any],
) -> bool:
    return _actual_mismatch(desired, config, raw_paths, raw_config, runtime_streams) is None


def _wait_for_camera_state(
    client: FrigateClient,
    desired: dict[str, Any],
    *,
    timeout: float = FRIGATE_VERIFY_TIMEOUT_SECONDS,
    poll_interval: float = FRIGATE_VERIFY_POLL_SECONDS,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            config = client.config()
            raw_paths = client.raw_paths()
            raw_config = client.raw_config()
            runtime_streams = client.runtime_streams()
            stats = client.stats()
        except FrigateApiError:
            if time.monotonic() >= deadline:
                raise
        else:
            mismatch = _actual_mismatch(
                desired,
                config,
                raw_paths,
                raw_config,
                runtime_streams,
            )
            if mismatch is None and desired["key"] not in stats:
                mismatch = "camera_start_pending"
            if mismatch is None:
                return config, raw_paths, raw_config, runtime_streams, stats
            if time.monotonic() >= deadline:
                raise FrigateApiError(mismatch)
        if poll_interval > 0:
            time.sleep(poll_interval)


def reconcile_frigate(
    repository: Any,
    target: FrigateTarget,
    *,
    media_host: str = "127.0.0.1",
    media_host_resolver: Callable[[str], str] | None = None,
    client_factory: Callable[[FrigateTarget], FrigateClient] = FrigateClient,
    allow_restart: bool = True,
    camera_uuid: str | None = None,
    verification_timeout: float = FRIGATE_VERIFY_TIMEOUT_SECONDS,
    verification_poll_interval: float = FRIGATE_VERIFY_POLL_SECONDS,
) -> dict[str, int]:
    cameras = selected_camera_inventory(repository, target.target_id)
    if camera_uuid is not None:
        cameras = [camera for camera in cameras if str(camera["camera_uuid"]) == camera_uuid]
    if not cameras:
        return {"applied": 0, "pending": 0}
    client = client_factory(target)
    client.capabilities()
    config = client.config()
    raw_paths = client.raw_paths()
    raw_config = client.raw_config()
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
                    raise FrigateApiError("camera_enabled_mismatch")
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
        camera_media_host = (
            media_host_resolver(str(camera["frigate_address_mode"]))
            if media_host_resolver is not None
            else media_host
        )
        desired = desired_camera(camera, password, camera_media_host)
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
        actual_matches = _actual_matches(
            desired,
            config,
            raw_paths,
            raw_config,
            runtime_streams,
        )
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
            # leaves the previous processes alive. Poll read-only state instead.
            repository.record_frigate_attempt(
                target.target_id,
                camera["camera_uuid"],
                desired["key"],
                desired["record_stream_uuid"],
                desired["detect_stream_uuid"],
                desired["desired_hash"],
            )
            try:
                verified_state = _wait_for_camera_state(
                    client,
                    desired,
                    timeout=verification_timeout,
                    poll_interval=verification_poll_interval,
                )
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
            config, raw_paths, raw_config, runtime_streams, stats = verified_state
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
                saved_camera = raw_config.get("cameras", {}).get(desired["key"], {})
                saved_detect = (
                    saved_camera.get("detect") if isinstance(saved_camera, dict) else None
                )
                if isinstance(saved_detect, dict) and "fps" in saved_detect:
                    camera_update = {
                        **camera_update,
                        "detect": {**camera_update["detect"], "fps": ""},
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
            if restart_for_second_camera and allow_restart:
                client.restart()
                time.sleep(FRIGATE_RESTART_SETTLE_SECONDS)
            verified_state = _wait_for_camera_state(
                client,
                desired,
                timeout=verification_timeout,
                poll_interval=verification_poll_interval,
            )
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
        config, raw_paths, raw_config, runtime_streams, stats = verified_state
        applied += 1
    return {"applied": applied, "pending": pending}
