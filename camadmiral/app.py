from __future__ import annotations

import asyncio
import ipaddress
import json
import hashlib
import os
import secrets
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

from .auth import AdminAuthenticator
from .config import SecretConfigurationError, database_path, read_secret_file, settings
from .crypto import load_master_key
from .diagnostics import snapshot
from .frigate import (
    FrigateApiError,
    load_frigate_targets,
    media_host_from_inventory,
    reconcile_frigate,
)
from .media import (
    ProbeResult,
    RelayHealthMonitor,
    SnapshotError,
    go2rtc_websocket_url,
    probe_source,
    reconcile_and_probe,
    reconcile_runtime_drift,
    snapshot_frame,
)
from .onvif_client import OnvifInspectionError, inspect_onvif_candidate
from .notifications import TelegramClient, TelegramError, notification_text, pairing_message
from .roles import select_stream_roles
from .rtsp_catalog import CatalogCandidate, CatalogError, catalog_candidates
from .recovery import recover_inventory_addresses
from .scan_state import preserve_inventory
from .storage import CameraRepository

app = FastAPI(
    title="CamAdmiral Discovery Preview",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

INDEX = Path(__file__).with_name("index.html")
SCAN_REQUEST = Path("/run/camadmiral/scan-request.json")
SCAN_STATE = Path("/run/camadmiral/scan-state.json")
INVENTORY = settings().storage.inventory
REPOSITORY: CameraRepository | None = None
MEDIA_LOCK = threading.Lock()
FRIGATE_LOCK = threading.Lock()
SCAN_REQUEST_LOCK = threading.Lock()
RELAY_HEALTH_MONITOR = RelayHealthMonitor()
HEALTH_INTERVAL = max(10.0, float(os.environ.get("CAMADMIRAL_HEALTH_INTERVAL", "30")))
RUNTIME_RECONCILE_INTERVAL = max(
    2.0,
    float(os.environ.get("CAMADMIRAL_RUNTIME_RECONCILE_INTERVAL", "5")),
)
RECOVERY_SCAN_INTERVAL = max(
    60.0,
    float(os.environ.get("CAMADMIRAL_RECOVERY_SCAN_INTERVAL", "300")),
)
RECOVERY_SCAN_ATTEMPTS: dict[str, float] = {}
FRIGATE_RECONCILE_INTERVAL = 30.0
NOTIFICATION_INTERVAL = 5.0


def _admin_password() -> bytes | None:
    return read_secret_file(settings().secrets.admin_password_file)


ADMIN_AUTH = AdminAuthenticator(_admin_password)


@app.middleware("http")
async def require_admin_authentication(request: Request, call_next):
    if request.url.path == "/healthz" or request.url.path.startswith("/api/v1/"):
        return await call_next(request)
    client = request.client.host if request.client else "unknown"
    decision = ADMIN_AUTH.authenticate(client, request.headers.get("Authorization"))
    if decision.allowed:
        return await call_next(request)
    headers = {"WWW-Authenticate": 'Basic realm="CamAdmiral", charset="UTF-8"'}
    if decision.retry_after is not None:
        headers["Retry-After"] = str(decision.retry_after)
    return Response(status_code=decision.status_code, headers=headers)


class AdoptionRequest(BaseModel):
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=512)
    allow_factory_credentials: bool = False


class RtspSourceRequest(BaseModel):
    label: str = Field(default="", max_length=80)
    url: str = Field(min_length=1, max_length=2048)


class RtspAdoptionRequest(BaseModel):
    display_name: str = Field(default="", max_length=160)
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=512)
    sources: list[RtspSourceRequest] = Field(default_factory=list, max_length=2)


class CameraUpdateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=160)


class CameraEnabledRequest(BaseModel):
    enabled: bool


class CameraCredentialRequest(BaseModel):
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=512)


class ExplicitAddressRequest(BaseModel):
    address: str = Field(min_length=7, max_length=45)


class NotificationSettingsRequest(BaseModel):
    # Retained for compatibility with existing clients. Telegram alerts are
    # enabled whenever a bot is configured, so callers no longer need to send it.
    enabled: bool = True
    telegram_bot_token: str | None = Field(default=None, min_length=20, max_length=256)


FACTORY_ONVIF_USERNAME = "admin"
FACTORY_ONVIF_PASSWORD = "admin"


def _factory_credentials(username: str, password: str) -> bool:
    return username.lower() == FACTORY_ONVIF_USERNAME and password == FACTORY_ONVIF_PASSWORD


def _factory_credentials_response() -> JSONResponse:
    return _secured_json(
        {
            "status": "factory_credentials_available",
            "message": (
                "This camera's ONVIF service accepts the factory credentials admin/admin. "
                "Anyone on this network may be able to access it."
            ),
        },
        status_code=409,
    )


def _repository(*, required: bool = False) -> CameraRepository | None:
    global REPOSITORY
    if REPOSITORY is not None:
        return REPOSITORY
    try:
        repository = CameraRepository(database_path(), load_master_key())
        repository.migrate()
    except SecretConfigurationError:
        if required:
            raise
        return None
    REPOSITORY = repository
    return repository


def _decorate_adoptions(state: dict[str, object]) -> dict[str, object]:
    repository = _repository()
    if repository is None:
        return state
    adoptions = repository.adoption_map()
    frigate_bindings = [
        (
            target,
            {
                str(binding["camera_uuid"]): binding
                for binding in repository.frigate_bindings(target.target_id)
            },
        )
        for target in load_frigate_targets()
    ]
    for device in state.get("devices", []):
        candidate_uuid = device.get("candidate_uuid")
        if candidate_uuid in adoptions:
            adoption = adoptions[candidate_uuid]
            adoption["frigate"] = []
            for target, bindings_by_camera in frigate_bindings:
                binding = bindings_by_camera.get(str(adoption["camera_uuid"]))
                target_status = {
                    "target": target.name,
                    "status": binding["status"] if binding is not None else "pending",
                }
                if binding is not None and binding.get("last_error_code"):
                    target_status["error_code"] = binding["last_error_code"]
                adoption["frigate"].append(target_status)
            device["adoption"] = adoption
            device["display_name"] = adoption["display_name"]
    return state


def _reconcile_media(*, wait: bool = True) -> bool:
    repository = _repository()
    if repository is None or not MEDIA_LOCK.acquire(blocking=wait):
        return False
    try:
        reconcile_and_probe(repository)
        return True
    except Exception as exc:
        print(f"media: reconciliation failed ({type(exc).__name__})", flush=True)
        return False
    finally:
        MEDIA_LOCK.release()


def _media_health_loop() -> None:
    while True:
        time.sleep(HEALTH_INTERVAL)
        repository = _repository()
        if repository is None or not MEDIA_LOCK.acquire(blocking=False):
            continue
        try:
            recovery_results = recover_inventory_addresses(repository, INVENTORY)
            for result in recovery_results:
                print(
                    "media: address recovery "
                    f"camera={result.camera_uuid} status={result.status} "
                    f"from={result.previous_address} to={result.current_address}",
                    flush=True,
                )
            RELAY_HEALTH_MONITOR.probe(repository)
            _queue_targeted_recovery_scan(repository)
        except Exception as exc:
            print(f"media: health probe failed ({type(exc).__name__})", flush=True)
        finally:
            MEDIA_LOCK.release()


def _media_runtime_reconciliation_loop() -> None:
    while True:
        time.sleep(RUNTIME_RECONCILE_INTERVAL)
        repository = _repository()
        if repository is None or not MEDIA_LOCK.acquire(blocking=False):
            continue
        try:
            if reconcile_runtime_drift(repository):
                print("media: restored managed streams after runtime drift", flush=True)
        except Exception:
            pass
        finally:
            MEDIA_LOCK.release()


def _reconcile_frigate(*, wait: bool = True) -> None:
    repository = _repository()
    if repository is None or not FRIGATE_LOCK.acquire(blocking=wait):
        return
    try:
        targets = load_frigate_targets()
        if not targets:
            return
        media_host = media_host_from_inventory(INVENTORY)
        for target in targets:
            try:
                reconcile_frigate(repository, target, media_host=media_host)
            except FrigateApiError as exc:
                print(
                    f"frigate[{target.target_id}]: reconciliation deferred ({exc.code})",
                    flush=True,
                )
    except Exception as exc:
        print(f"frigate: reconciliation failed ({type(exc).__name__})", flush=True)
    finally:
        FRIGATE_LOCK.release()


def _frigate_reconciliation_loop() -> None:
    while True:
        _reconcile_frigate(wait=False)
        time.sleep(FRIGATE_RECONCILE_INTERVAL)


def _queue_frigate_reconciliation() -> None:
    threading.Thread(
        target=_reconcile_frigate,
        kwargs={"wait": False},
        name="frigate-reconcile-now",
        daemon=True,
    ).start()


def _notification_settings_payload(repository: CameraRepository) -> dict[str, object]:
    payload = repository.notification_settings()
    credentials = repository.notification_credentials()
    if credentials and credentials.get("pairing_token") and credentials.get("bot_username"):
        expires_at = credentials.get("pairing_expires_at")
        if expires_at:
            expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expires > datetime.now(timezone.utc):
                username = urllib.parse.quote(str(credentials["bot_username"]), safe="_")
                token = urllib.parse.quote(str(credentials["pairing_token"]), safe="-_")
                payload["connect_url"] = f"https://t.me/{username}?start={token}"
                payload["pairing_expires_at"] = expires.isoformat()
    return payload


def _poll_telegram_pairing(repository: CameraRepository, credentials: dict[str, object]) -> None:
    token = credentials.get("bot_token")
    pairing_token = credentials.get("pairing_token")
    expires_at = credentials.get("pairing_expires_at")
    if not token or not pairing_token or credentials.get("chat_id") or not expires_at:
        return
    expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    if expires <= datetime.now(timezone.utc):
        return
    updates = TelegramClient(str(token)).updates(credentials.get("update_offset"))
    next_offset = credentials.get("update_offset")
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            next_offset = max(int(next_offset or 0), update_id + 1)
        destination = pairing_message(update, str(pairing_token))
        if destination is not None:
            repository.complete_telegram_pairing(
                chat_id=destination[0],
                chat_label=destination[1],
                update_offset=int(next_offset or 0),
            )
            return
    if next_offset is not None and next_offset != credentials.get("update_offset"):
        repository.update_telegram_offset(int(next_offset))


def _deliver_notification(
    repository: CameraRepository,
    credentials: dict[str, object],
    item: dict[str, object],
) -> None:
    token = credentials.get("bot_token")
    chat_id = credentials.get("chat_id")
    if not token or not chat_id:
        return
    try:
        message_id = TelegramClient(str(token)).send(
            str(chat_id),
            notification_text(str(item["event_type"]), dict(item["payload"])),
        )
    except TelegramError as exc:
        repository.fail_notification_delivery(
            str(item["outbox_uuid"]),
            error_code=exc.code,
            retry_after_seconds=exc.retry_after_seconds,
        )
        raise
    repository.complete_notification_delivery(
        str(item["outbox_uuid"]),
        message_id=message_id,
    )


def _notification_loop() -> None:
    while True:
        time.sleep(NOTIFICATION_INTERVAL)
        repository = _repository()
        if repository is None:
            continue
        credentials = repository.notification_credentials()
        if credentials is None or not credentials.get("bot_token"):
            continue
        try:
            if not credentials.get("chat_id"):
                _poll_telegram_pairing(repository, credentials)
                continue
            if not credentials.get("enabled"):
                continue
            for item in repository.due_notifications():
                try:
                    _deliver_notification(repository, credentials, item)
                except TelegramError:
                    continue
        except Exception as exc:
            print(f"notifications: delivery deferred ({type(exc).__name__})", flush=True)


def _queue_targeted_recovery_scan(repository: CameraRepository) -> bool:
    if SCAN_REQUEST.exists():
        return False
    state = _read_scan_state()
    if state.get("status") in {"queued", "running"}:
        return False
    try:
        inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    candidates = {
        str(candidate.get("candidate_uuid")): candidate
        for candidate in inventory.get("devices", [])
        if candidate.get("candidate_uuid")
    }
    now = time.monotonic()
    targets: list[dict[str, object]] = []
    for candidate_uuid, adoption in sorted(repository.adoption_map().items()):
        if not adoption.get("enabled", True):
            continue
        streams = adoption.get("streams", [])
        if not streams or not any(stream.get("health_status") == "offline" for stream in streams):
            continue
        if any(stream.get("health_status") == "auth_failed" for stream in streams):
            continue
        previous_attempt = RECOVERY_SCAN_ATTEMPTS.get(candidate_uuid)
        if previous_attempt is not None and now - previous_attempt < RECOVERY_SCAN_INTERVAL:
            continue
        candidate = candidates.get(candidate_uuid) or {}
        onvif = candidate.get("onvif") or {}
        endpoint_reference = onvif.get("endpoint_reference")
        mac = candidate.get("mac")
        if not endpoint_reference and not mac:
            continue
        targets.append(
            {
                "candidate_uuid": candidate_uuid,
                "endpoint_reference": endpoint_reference,
                "mac": mac,
            }
        )
        if len(targets) >= 16:
            break
    if not targets:
        return False
    request = {
        "scan_id": str(uuid.uuid4()),
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "unix_time": time.time(),
        "mode": "targeted",
        "targets": targets,
    }
    with SCAN_REQUEST_LOCK:
        if SCAN_REQUEST.exists():
            return False
        temporary = SCAN_REQUEST.with_suffix(".tmp")
        temporary.write_text(json.dumps(request), encoding="utf-8")
        temporary.replace(SCAN_REQUEST)
    for target in targets:
        RECOVERY_SCAN_ATTEMPTS[str(target["candidate_uuid"])] = now
    return True


@app.on_event("startup")
def start_media_reconciliation() -> None:
    threading.Thread(
        target=_reconcile_media,
        kwargs={"wait": False},
        name="media-reconcile",
        daemon=True,
    ).start()
    threading.Thread(
        target=_media_runtime_reconciliation_loop,
        name="media-runtime-reconcile",
        daemon=True,
    ).start()
    threading.Thread(
        target=_media_health_loop,
        name="media-health",
        daemon=True,
    ).start()
    threading.Thread(
        target=_frigate_reconciliation_loop,
        name="frigate-reconcile",
        daemon=True,
    ).start()
    threading.Thread(
        target=_notification_loop,
        name="notification-delivery",
        daemon=True,
    ).start()


def _secured_json(payload: dict[str, object], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


def _consumer_authentication_error(authorization: str | None) -> JSONResponse | None:
    try:
        expected = read_secret_file(settings().secrets.api_token_file, required=True)
    except SecretConfigurationError:
        return _secured_json(
            {"detail": "Consumer API authentication is not configured"},
            status_code=503,
        )
    assert expected is not None
    scheme, separator, supplied = (authorization or "").partition(" ")
    authorized = (
        separator == " "
        and scheme.lower() == "bearer"
        and bool(supplied)
        and len(supplied) <= 8192
        and secrets.compare_digest(supplied.encode("utf-8"), expected)
    )
    if authorized:
        return None
    response = _secured_json({"detail": "Unauthorized"}, status_code=401)
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _inventory_candidates() -> dict[str, dict[str, object]]:
    try:
        inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    devices = inventory.get("devices", [])
    if not isinstance(devices, list):
        return {}
    return {
        str(device["candidate_uuid"]): device
        for device in devices
        if isinstance(device, dict) and device.get("candidate_uuid")
    }


def _consumer_stream_state(health_status: object) -> str:
    if health_status == "healthy":
        return "healthy"
    if health_status in {None, "", "unknown"}:
        return "unknown"
    return "unhealthy"


def _consumer_camera_state(streams: list[dict[str, object]], *, enabled: bool = True) -> str:
    if not enabled:
        return "disabled"
    states = [_consumer_stream_state(stream.get("health_status")) for stream in streams]
    if states and all(state == "healthy" for state in states):
        return "online"
    if any(state in {"healthy", "unknown"} for state in states):
        return "degraded"
    return "offline"


def _consumer_video(stream: dict[str, object]) -> dict[str, object]:
    width = int(stream.get("probed_width") or stream.get("width") or 0)
    height = int(stream.get("probed_height") or stream.get("height") or 0)
    fps = float(stream.get("probed_fps") or stream.get("fps") or 0)
    codec = str(stream.get("video_codec") or stream.get("encoding") or "").lower()
    return {"codec": codec, "width": width, "height": height, "fps": fps}


def _consumer_rtsp_base(request: Request) -> str:
    hostname = request.url.hostname or "127.0.0.1"
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"rtsp://{host}:18554"


def _consumer_onvif(candidate: dict[str, object] | None) -> dict[str, object]:
    onvif = candidate.get("onvif") if candidate else None
    service_urls = onvif.get("service_urls", []) if isinstance(onvif, dict) else []
    service_url = next((str(value) for value in service_urls if value), None)
    return {"available": service_url is not None, "device_service_url": service_url}


@app.get("/api/v1/cameras", include_in_schema=False)
def consumer_cameras(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    authentication_error = _consumer_authentication_error(authorization)
    if authentication_error is not None:
        return authentication_error
    try:
        repository = _repository(required=True)
    except SecretConfigurationError:
        return _secured_json({"detail": "Camera inventory is not configured"}, status_code=503)
    assert repository is not None
    candidates = _inventory_candidates()
    rtsp_base = _consumer_rtsp_base(request)
    rtsp_password = repository.rtsp_access_password()
    cameras = []
    for camera in repository.consumer_inventory():
        enabled = bool(camera.get("enabled", True))
        streams = camera["streams"] if enabled else []
        public_streams = []
        for stream in streams:
            public_streams.append(
                {
                    "id": stream["stream_uuid"],
                    "roles": stream["roles"],
                    "state": _consumer_stream_state(stream.get("health_status")),
                    "video": _consumer_video(stream),
                    "downstream": {
                        "protocol": "rtsp",
                        "url": f'{rtsp_base}/{stream["stream_key"]}',
                        "authentication": {
                            "type": "username_password",
                            "username": "camadmiral",
                            "password": rtsp_password,
                        },
                    },
                }
            )
        cameras.append(
            {
                "id": camera["camera_uuid"],
                "name": camera["display_name"],
                "state": _consumer_camera_state(streams, enabled=enabled),
                "onvif": _consumer_onvif(candidates.get(camera["candidate_uuid"])),
                "streams": public_streams,
            }
        )
    return _secured_json({"api_version": "1", "cameras": cameras})


def _camera_adoption(repository: CameraRepository, camera_uuid: str) -> dict[str, object] | None:
    camera = repository.camera(camera_uuid)
    if camera is None:
        return None
    return repository.adoption_for_candidate(str(camera["candidate_uuid"]))


def _validate_saved_camera_sources(
    repository: CameraRepository,
    camera_uuid: str,
) -> tuple[bool, str]:
    sources = repository.managed_stream_sources(
        include_disabled=True,
        camera_uuid=camera_uuid,
        role_bound_only=True,
    )
    if not sources:
        return False, "No saved recording or detection streams are available."
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(sources)))) as executor:
        futures = [
            executor.submit(
                probe_source,
                source["uri"],
                source["username"],
                source["password"],
            )
            for source in sources
        ]
        results = [future.result() for future in futures]
    repository.record_probe_results(
        {source["stream_uuid"]: result for source, result in zip(sources, results)}
    )
    if all(result.status == "ready" for result in results):
        return True, ""
    if any(result.status == "auth_failed" for result in results):
        return False, "Saved camera credentials are no longer accepted."
    return False, "Saved camera streams are unavailable."


def _validate_replacement_credentials(
    repository: CameraRepository,
    camera_uuid: str,
    username: str,
    password: str,
) -> tuple[bool, str, str]:
    adoption = _camera_adoption(repository, camera_uuid)
    if adoption is None:
        return False, "camera_not_found", "Adopted camera not found."
    streams = adoption.get("streams", [])
    if any(stream.get("source_kind") == "onvif" for stream in streams):
        candidate = _find_candidate(str(adoption["candidate_uuid"]))
        if candidate is None or candidate.get("status") != "online":
            return False, "camera_unavailable", "Camera is offline."
        try:
            inspection = inspect_onvif_candidate(
                candidate,
                username=username if username else None,
                password=password if username else None,
            )
        except OnvifInspectionError as exc:
            if exc.code == "credentials_required":
                return False, "credentials_required", "Incorrect username or password."
            if exc.code == "unreachable":
                return False, "camera_unavailable", "Camera is unavailable."
            return False, "validation_failed", "ONVIF credential validation failed."
        returned_tokens = {str(profile["token"]) for profile in inspection.get("profiles", [])}
        required_tokens = {str(token) for token in adoption.get("role_tokens", {}).values()}
        if not required_tokens.issubset(returned_tokens):
            return (
                False,
                "profiles_changed",
                "Camera profiles changed. Inspect and adopt the camera again before replacing credentials.",
            )
    sources = repository.managed_stream_sources(
        include_disabled=True,
        camera_uuid=camera_uuid,
        role_bound_only=True,
    )
    if not sources:
        return False, "media_unavailable", "No saved recording or detection streams are available."
    results = _probe_exact_sources(
        [source["uri"] for source in sources],
        username,
        password,
    )
    if all(result.status == "ready" for result in results):
        return True, "ok", ""
    if any(result.status == "auth_failed" for result in results):
        return False, "credentials_required", "Incorrect username or password."
    return False, "media_unavailable", "Camera streams are unavailable."


@app.get("/internal/cameras/{camera_uuid}/availability", include_in_schema=False)
def camera_availability(camera_uuid: str, window: str = "24h") -> JSONResponse:
    windows = {
        "24h": (24, 48),
        "7d": (24 * 7, 56),
    }
    selected = windows.get(window)
    if selected is None:
        return _secured_json(
            {"status": "invalid_window", "message": "Choose the 24-hour or 7-day view."},
            status_code=422,
        )
    repository = _repository(required=True)
    assert repository is not None
    result = repository.camera_availability(
        camera_uuid,
        hours=selected[0],
        bucket_count=selected[1],
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Adopted camera not found")
    result["window"] = window
    return _secured_json(result)


@app.get("/internal/incidents", include_in_schema=False)
def incidents(status: str = "open", limit: int = 50) -> JSONResponse:
    if status not in {"open", "resolved", "all"}:
        return _secured_json(
            {"status": "invalid_status", "message": "Choose open, resolved, or all incidents."},
            status_code=422,
        )
    if limit < 1 or limit > 100:
        return _secured_json(
            {"status": "invalid_limit", "message": "Choose a limit from 1 to 100."},
            status_code=422,
        )
    repository = _repository(required=True)
    assert repository is not None
    return _secured_json(repository.incidents(status=status, limit=limit))


@app.get("/internal/notification-settings", include_in_schema=False)
def notification_settings() -> JSONResponse:
    repository = _repository(required=True)
    assert repository is not None
    return _secured_json(_notification_settings_payload(repository))


@app.post("/internal/notification-settings", include_in_schema=False)
def update_notification_settings(
    request: NotificationSettingsRequest,
    x_camadmiral_action: str | None = Header(default=None),
) -> JSONResponse:
    if x_camadmiral_action != "update-notification-settings":
        raise HTTPException(status_code=400, detail="Missing notification settings action header")
    repository = _repository(required=True)
    assert repository is not None
    credentials = repository.notification_credentials()
    token = request.telegram_bot_token.strip() if request.telegram_bot_token else None
    if token is None and (credentials is None or not credentials.get("bot_token")):
        return _secured_json(
            {"status": "bot_token_required", "message": "Paste a Telegram bot token first."},
            status_code=422,
        )

    bot_id = None
    bot_username = None
    if token is not None:
        client = TelegramClient(token)
        try:
            identity = client.identity()
            webhook = client.webhook()
        except TelegramError as exc:
            message = (
                "Telegram did not accept that bot token. Copy it again from BotFather."
                if exc.code == "invalid_bot_token"
                else "Telegram is unavailable. Try again shortly."
            )
            return _secured_json({"status": exc.code, "message": message}, status_code=422 if exc.retry_after_seconds is None else 503)
        if webhook.get("url"):
            return _secured_json(
                {
                    "status": "bot_has_webhook",
                    "message": "This bot is connected to another application. Create a dedicated CamAdmiral bot.",
                },
                status_code=409,
            )
        bot_id = str(identity["id"])
        bot_username = str(identity["username"])

    disconnected = token is not None or not credentials or not credentials.get("chat_id")
    pairing_token = secrets.token_urlsafe(24) if disconnected else None
    pairing_expires_at = (
        (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        if disconnected
        else None
    )
    repository.save_telegram_settings(
        enabled=True,
        bot_token=token,
        bot_id=bot_id,
        bot_username=bot_username,
        pairing_token=pairing_token,
        pairing_expires_at=pairing_expires_at,
    )
    return _secured_json(_notification_settings_payload(repository))


@app.post("/internal/notification-settings/test", include_in_schema=False)
def test_notification_settings(
    x_camadmiral_action: str | None = Header(default=None),
) -> JSONResponse:
    if x_camadmiral_action != "test-notification-settings":
        raise HTTPException(status_code=400, detail="Missing notification test action header")
    repository = _repository(required=True)
    assert repository is not None
    credentials = repository.notification_credentials()
    if not credentials or not credentials.get("bot_token") or not credentials.get("chat_id"):
        return _secured_json(
            {"status": "telegram_not_connected", "message": "Connect a Telegram chat first."},
            status_code=409,
        )
    item = repository.enqueue_test_notification()
    try:
        _deliver_notification(repository, credentials, item)
    except TelegramError as exc:
        return _secured_json(
            {"status": exc.code, "message": "Telegram could not deliver the test notification."},
            status_code=503,
        )
    return _secured_json(
        {"status": "sent", "message": "Test notification sent.", "settings": _notification_settings_payload(repository)}
    )


@app.post("/internal/cameras/{camera_uuid}/update", include_in_schema=False)
def update_camera(
    camera_uuid: str,
    request: CameraUpdateRequest,
    x_camadmiral_action: str | None = Header(default=None),
) -> JSONResponse:
    if x_camadmiral_action != "update-camera":
        raise HTTPException(status_code=400, detail="Missing camera update action header")
    display_name = request.display_name.strip()
    if not display_name:
        return _secured_json(
            {"status": "invalid_name", "message": "Enter a camera name."},
            status_code=422,
        )
    repository = _repository(required=True)
    assert repository is not None
    if not repository.update_camera_name(camera_uuid, display_name):
        raise HTTPException(status_code=404, detail="Adopted camera not found")
    _queue_frigate_reconciliation()
    return _secured_json(
        {"status": "updated", "camera": _camera_adoption(repository, camera_uuid)}
    )


@app.post("/internal/cameras/{camera_uuid}/enabled", include_in_schema=False)
def set_camera_enabled(
    camera_uuid: str,
    request: CameraEnabledRequest,
    x_camadmiral_action: str | None = Header(default=None),
) -> JSONResponse:
    if x_camadmiral_action != "set-camera-enabled":
        raise HTTPException(status_code=400, detail="Missing camera state action header")
    repository = _repository(required=True)
    assert repository is not None
    camera = repository.camera(camera_uuid)
    if camera is None:
        raise HTTPException(status_code=404, detail="Adopted camera not found")
    if bool(camera["enabled"]) == request.enabled:
        return _secured_json(
            {
                "status": "enabled" if request.enabled else "disabled",
                "camera": _camera_adoption(repository, camera_uuid),
            }
        )
    if request.enabled:
        valid, message = _validate_saved_camera_sources(repository, camera_uuid)
        if not valid:
            return _secured_json(
                {"status": "camera_unavailable", "message": message},
                status_code=409,
            )
    repository.set_camera_enabled(camera_uuid, request.enabled)
    if not _reconcile_media():
        if request.enabled:
            repository.set_camera_enabled(camera_uuid, False)
            _reconcile_media()
        return _secured_json(
            {
                "status": "media_update_failed",
                "message": "Camera state was saved, but the media service could not apply it.",
                "camera": _camera_adoption(repository, camera_uuid),
            },
            status_code=503,
        )
    _reconcile_frigate()
    return _secured_json(
        {
            "status": "enabled" if request.enabled else "disabled",
            "camera": _camera_adoption(repository, camera_uuid),
        }
    )


@app.post("/internal/cameras/{camera_uuid}/credentials", include_in_schema=False)
def update_camera_credentials(
    camera_uuid: str,
    request: CameraCredentialRequest,
    x_camadmiral_action: str | None = Header(default=None),
) -> JSONResponse:
    if x_camadmiral_action != "update-camera-credentials":
        raise HTTPException(status_code=400, detail="Missing credential update action header")
    repository = _repository(required=True)
    assert repository is not None
    camera = repository.camera(camera_uuid)
    if camera is None:
        raise HTTPException(status_code=404, detail="Adopted camera not found")
    username = request.username.strip()
    valid, status, message = _validate_replacement_credentials(
        repository,
        camera_uuid,
        username,
        request.password,
    )
    if not valid:
        code = 401 if status == "credentials_required" else 409
        return _secured_json({"status": status, "message": message}, status_code=code)
    repository.replace_camera_credentials(camera_uuid, username, request.password)
    if bool(camera["enabled"]) and not _reconcile_media():
        return _secured_json(
            {
                "status": "media_update_failed",
                "message": "Credentials were saved, but the media service could not reconnect.",
                "camera": _camera_adoption(repository, camera_uuid),
            },
            status_code=503,
        )
    return _secured_json(
        {
            "status": "credentials_updated",
            "camera": _camera_adoption(repository, camera_uuid),
        }
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(
        INDEX,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' data: blob:; media-src 'self' blob:; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


@app.get("/internal/status", include_in_schema=False)
def internal_status() -> JSONResponse:
    return _secured_json(snapshot())


@app.post("/internal/media/access", include_in_schema=False)
def media_access(
    x_camadmiral_action: str | None = Header(default=None),
) -> JSONResponse:
    if x_camadmiral_action != "reveal-media-access":
        raise HTTPException(status_code=400, detail="Missing media access action header")
    try:
        repository = _repository(required=True)
    except SecretConfigurationError:
        return _secured_json(
            {"status": "not_configured", "message": "Media access is not configured."},
            status_code=503,
        )
    assert repository is not None
    return _secured_json(
        {
            "status": "ok",
            "username": "camadmiral",
            "password": repository.rtsp_access_password(),
            "port": 18554,
        }
    )


def _snapshot_response(content: bytes = b"", *, status_code: int = 200) -> Response:
    return Response(
        content=content,
        status_code=status_code,
        media_type="image/jpeg" if status_code == 200 else None,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


@app.get("/internal/cameras/{camera_uuid}/snapshot.jpg", include_in_schema=False)
def camera_snapshot(camera_uuid: str) -> Response:
    try:
        repository = _repository(required=True)
    except SecretConfigurationError:
        return _snapshot_response(status_code=503)
    assert repository is not None
    stream = repository.preview_stream_for_camera(camera_uuid)
    if stream is None:
        return _snapshot_response(status_code=404)
    try:
        return _snapshot_response(snapshot_frame(stream["stream_key"]))
    except SnapshotError:
        return _snapshot_response(status_code=503)


def _same_websocket_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not origin or not host:
        return False
    parsed = urllib.parse.urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host.lower()


def _live_control_message(message: str) -> str:
    if len(message) > 4096:
        raise ValueError("Live control message is too large")
    payload = json.loads(message)
    if not isinstance(payload, dict) or payload.get("type") not in {"mse", "mjpeg"}:
        raise ValueError("Unsupported live control message")
    value = payload.get("value")
    if value is not None and (not isinstance(value, str) or len(value) > 1024):
        raise ValueError("Invalid live control value")
    normalized = {"type": payload["type"]}
    if value is not None:
        normalized["value"] = value
    return json.dumps(normalized, separators=(",", ":"))


@app.websocket("/internal/cameras/{camera_uuid}/live")
async def camera_live(websocket: WebSocket, camera_uuid: str) -> None:
    client = websocket.client.host if websocket.client else "unknown"
    decision = ADMIN_AUTH.authenticate(client, websocket.headers.get("Authorization"))
    if not decision.allowed:
        await websocket.close(code=4429 if decision.status_code == 429 else 4401)
        return
    if not _same_websocket_origin(websocket):
        await websocket.close(code=4403)
        return
    try:
        repository = _repository(required=True)
    except SecretConfigurationError:
        await websocket.close(code=1013)
        return
    assert repository is not None
    stream = repository.preview_stream_for_camera(camera_uuid)
    if stream is None:
        await websocket.close(code=4404)
        return

    try:
        async with websocket_connect(
            go2rtc_websocket_url(stream["stream_key"]),
            open_timeout=5,
            close_timeout=2,
            max_size=16 * 1024 * 1024,
            max_queue=16,
        ) as upstream:
            await websocket.accept()

            async def browser_to_relay() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    text = message.get("text")
                    if text is None:
                        await websocket.close(code=1003)
                        return
                    try:
                        await upstream.send(_live_control_message(text))
                    except (ValueError, json.JSONDecodeError):
                        await websocket.close(code=1008)
                        return

            async def relay_to_browser() -> None:
                while True:
                    message = await upstream.recv()
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            tasks = {
                asyncio.create_task(browser_to_relay()),
                asyncio.create_task(relay_to_browser()),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exception = task.exception()
                if exception is not None:
                    raise exception
    except (ConnectionClosed, WebSocketDisconnect):
        return
    except Exception:
        try:
            await websocket.close(code=1013)
        except RuntimeError:
            pass


def _read_scan_state() -> dict[str, object]:
    try:
        state = json.loads(SCAN_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        state = {"status": "starting", "phase": "starting"}
    if state.get("status") not in {"queued", "running"} or state.get("devices"):
        return state
    try:
        inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        inventory = {}
    return preserve_inventory(state, inventory)


def _find_candidate(candidate_uuid: str) -> dict[str, object] | None:
    try:
        inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    for candidate in inventory.get("devices", []):
        if candidate.get("candidate_uuid") == candidate_uuid:
            return candidate
    return None


def _stored_adoption_inspection(adoption: dict[str, object]) -> dict[str, object]:
    profiles = [
        {
            "token": stream["profile_token"],
            "name": stream["name"],
            "uri": stream["uri"],
            "width": stream["width"],
            "height": stream["height"],
            "encoding": stream["encoding"],
            "fps": stream["fps"],
            "source_kind": stream["source_kind"],
            "catalog_revision": stream.get("catalog_revision"),
            "catalog_rule_id": stream.get("catalog_rule_id"),
        }
        for stream in adoption.get("streams", [])
    ]
    return {
        "status": "adopted",
        "profiles": profiles,
        "adoption": adoption,
        "role_tokens": adoption.get("role_tokens", {}),
    }


def _normalize_rtsp_url(candidate: dict[str, object], value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port or 554
    except ValueError as exc:
        raise ValueError("Enter a valid RTSP URL.") from exc
    if parsed.scheme.lower() != "rtsp" or not parsed.hostname:
        raise ValueError("Stream URLs must start with rtsp:// and include a host.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Enter the username and password in their separate fields.")
    if parsed.fragment:
        raise ValueError("RTSP stream URLs cannot include a fragment.")
    if not 1 <= port <= 65535:
        raise ValueError("Enter a valid RTSP port.")
    candidate_ip = str(candidate.get("ip") or "")
    if parsed.hostname.lower() != candidate_ip.lower():
        raise ValueError(f"The stream URL must use the discovered camera address {candidate_ip}.")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit(("rtsp", host, parsed.path or "/", parsed.query, ""))


def _manual_profile(
    label: str,
    uri: str,
    result: ProbeResult,
    position: int,
    catalog: CatalogCandidate | None = None,
) -> dict[str, object]:
    token = "manual-" + hashlib.sha256(uri.encode("utf-8")).hexdigest()[:20]
    return {
        "token": token,
        "name": label.strip() or f"Stream {position}",
        "uri": uri,
        "width": result.width,
        "height": result.height,
        "encoding": (result.video_codec or "").upper(),
        "fps": result.fps,
        "bitrate_kbps": 0,
        "source_kind": "catalog_rtsp" if catalog else "manual_rtsp",
        "catalog_revision": catalog.catalog_revision if catalog else None,
        "catalog_rule_id": catalog.rule_id if catalog else None,
        "catalog_source_url": catalog.source_url if catalog else None,
    }


def _probe_exact_sources(
    uris: list[str],
    username: str,
    password: str,
) -> list[ProbeResult]:
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(uris)))) as executor:
        futures = [executor.submit(probe_source, uri, username, password) for uri in uris]
        return [future.result() for future in futures]


def _probe_catalog_sources(
    candidates: list[CatalogCandidate],
    username: str,
    password: str,
) -> list[ProbeResult]:
    if not candidates:
        return []
    first = probe_source(candidates[0].uri, username, password)
    if first.status == "auth_failed" or len(candidates) == 1:
        return [first]
    return [first, *_probe_exact_sources(
        [candidate.uri for candidate in candidates[1:]],
        username,
        password,
    )]


@app.get("/internal/discovery", include_in_schema=False)
def discovery_state() -> JSONResponse:
    return _secured_json(_decorate_adoptions(_read_scan_state()))


@app.post("/internal/discovery/scan", include_in_schema=False)
def start_discovery(
    x_camadmiral_action: str | None = Header(default=None),
) -> JSONResponse:
    if x_camadmiral_action != "scan":
        raise HTTPException(status_code=400, detail="Missing scan action header")
    with SCAN_REQUEST_LOCK:
        state = _read_scan_state()
        if state.get("status") in {"queued", "running"} or SCAN_REQUEST.exists():
            return _secured_json(_decorate_adoptions(state), status_code=409)
        scan_id = str(uuid.uuid4())
        request = {
            "scan_id": scan_id,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "unix_time": time.time(),
        }
        temporary = SCAN_REQUEST.with_suffix(".tmp")
        temporary.write_text(json.dumps(request), encoding="utf-8")
        temporary.replace(SCAN_REQUEST)
    queued = {"status": "queued", "phase": "queued", **request}
    if state.get("devices"):
        queued.update(
            {
                "inventory_scan_id": state.get("inventory_scan_id") or state.get("scan_id"),
                "devices": state.get("devices"),
                "summary": state.get("summary"),
                "network": state.get("network"),
                "duration_ms": state.get("duration_ms"),
                "completed_at": state.get("completed_at"),
                "raw_log": state.get("raw_log", []),
            }
        )
    return _secured_json(_decorate_adoptions(queued), status_code=202)


@app.post("/internal/discovery/address", include_in_schema=False)
def add_discovery_address(
    request: ExplicitAddressRequest,
    x_camadmiral_action: str | None = Header(default=None),
) -> JSONResponse:
    if x_camadmiral_action != "scan-address":
        raise HTTPException(status_code=400, detail="Missing address scan action header")
    try:
        address = ipaddress.IPv4Address(request.address.strip())
    except ipaddress.AddressValueError:
        return _secured_json(
            {"status": "invalid_address", "message": "Enter a valid private IPv4 address."},
            status_code=422,
        )
    if not address.is_private:
        return _secured_json(
            {"status": "invalid_address", "message": "Only private LAN addresses can be probed."},
            status_code=422,
        )
    with SCAN_REQUEST_LOCK:
        state = _read_scan_state()
        if state.get("status") in {"queued", "running"} or SCAN_REQUEST.exists():
            return _secured_json(_decorate_adoptions(state), status_code=409)
        scan_id = str(uuid.uuid4())
        payload = {
            "scan_id": scan_id,
            "mode": "address",
            "address": str(address),
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "unix_time": time.time(),
        }
        temporary = SCAN_REQUEST.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(SCAN_REQUEST)
    queued = {"status": "queued", "phase": "queued", **payload}
    if state.get("devices"):
        queued.update(
            {
                "inventory_scan_id": state.get("inventory_scan_id") or state.get("scan_id"),
                "devices": state.get("devices"),
                "summary": state.get("summary"),
                "network": state.get("network"),
                "raw_log": state.get("raw_log", []),
            }
        )
    return _secured_json(_decorate_adoptions(queued), status_code=202)


@app.post("/internal/discovery/{candidate_uuid}/inspect", include_in_schema=False)
def inspect_candidate(
    candidate_uuid: str,
    x_camadmiral_action: str | None = Header(default=None),
) -> JSONResponse:
    if x_camadmiral_action != "inspect":
        raise HTTPException(status_code=400, detail="Missing inspect action header")
    candidate = _find_candidate(candidate_uuid)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Camera candidate not found")
    if candidate.get("status") != "online":
        return _secured_json(
            {"status": "offline", "message": "Camera is offline"},
            status_code=409,
        )
    credentials = None
    repository = _repository()
    if repository is not None:
        credentials = repository.credentials_for_candidate(candidate_uuid)
    try:
        result = inspect_onvif_candidate(candidate, username=credentials[0], password=credentials[1]) if credentials else inspect_onvif_candidate(candidate)
    except OnvifInspectionError as exc:
        if repository is not None:
            adoption = repository.adoption_for_candidate(candidate_uuid)
            if adoption is not None:
                return _secured_json(_stored_adoption_inspection(adoption))
        expected = {"credentials_required", "not_onvif", "no_profiles"}
        code = 200 if exc.code in expected else 503 if exc.code == "unreachable" else 422
        return _secured_json(
            {"status": exc.code, "message": exc.message},
            status_code=code,
        )
    if repository is not None:
        adoption = repository.adoption_for_candidate(candidate_uuid)
        if adoption is not None:
            result["adoption"] = adoption
            result["role_tokens"] = adoption["role_tokens"]
    return _secured_json(result)


@app.post("/internal/discovery/{candidate_uuid}/adopt", include_in_schema=False)
def adopt_candidate(
    candidate_uuid: str,
    request: AdoptionRequest,
    x_camadmiral_action: str | None = Header(default=None),
) -> JSONResponse:
    if x_camadmiral_action != "adopt":
        raise HTTPException(status_code=400, detail="Missing adopt action header")
    candidate = _find_candidate(candidate_uuid)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Camera candidate not found")
    if candidate.get("status") != "online":
        return _secured_json({"status": "offline", "message": "Camera is offline"}, status_code=409)
    try:
        repository = _repository(required=True)
    except SecretConfigurationError:
        return _secured_json(
            {"status": "not_configured", "message": "Credential storage is not configured."},
            status_code=503,
        )
    assert repository is not None
    submitted_username = request.username.strip()
    submitted_password = request.password
    submitted_factory_credentials = _factory_credentials(
        submitted_username,
        submitted_password,
    )
    use_factory_credentials = request.allow_factory_credentials
    username = FACTORY_ONVIF_USERNAME if use_factory_credentials else submitted_username
    password = FACTORY_ONVIF_PASSWORD if use_factory_credentials else submitted_password
    try:
        result = inspect_onvif_candidate(
            candidate,
            username=username if username else None,
            password=password if username else None,
        )
    except OnvifInspectionError as exc:
        if (
            exc.code == "credentials_required"
            and not request.allow_factory_credentials
            and not submitted_factory_credentials
        ):
            try:
                inspect_onvif_candidate(
                    candidate,
                    username=FACTORY_ONVIF_USERNAME,
                    password=FACTORY_ONVIF_PASSWORD,
                )
            except OnvifInspectionError:
                pass
            else:
                return _factory_credentials_response()
        code = 401 if exc.code == "credentials_required" else 503 if exc.code == "unreachable" else 422
        return _secured_json({"status": exc.code, "message": exc.message}, status_code=code)
    if submitted_factory_credentials and not request.allow_factory_credentials:
        return _factory_credentials_response()
    roles = select_stream_roles(result["profiles"])
    if not roles:
        return _secured_json(
            {"status": "no_usable_streams", "message": "No usable ONVIF RTSP streams were reported."},
            status_code=422,
        )
    adoption = repository.adopt(candidate, username, password, result["profiles"], roles)
    _reconcile_media()
    _queue_frigate_reconciliation()
    adoption = repository.adoption_for_candidate(candidate_uuid) or adoption
    return _secured_json(
        {
            **result,
            "status": "adopted",
            "adoption": adoption,
            "role_tokens": roles,
        }
    )


@app.post("/internal/discovery/{candidate_uuid}/adopt-rtsp", include_in_schema=False)
def adopt_rtsp_candidate(
    candidate_uuid: str,
    request: RtspAdoptionRequest,
    x_camadmiral_action: str | None = Header(default=None),
) -> JSONResponse:
    if x_camadmiral_action != "adopt-rtsp":
        raise HTTPException(status_code=400, detail="Missing RTSP adoption action header")
    candidate = _find_candidate(candidate_uuid)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Camera candidate not found")
    if candidate.get("status") != "online":
        return _secured_json({"status": "offline", "message": "Camera is offline"}, status_code=409)
    try:
        repository = _repository(required=True)
    except SecretConfigurationError:
        return _secured_json(
            {"status": "not_configured", "message": "Credential storage is not configured."},
            status_code=503,
        )
    assert repository is not None
    username = request.username.strip()
    automatic = not request.sources
    catalog_entries: list[CatalogCandidate] = []
    if automatic:
        try:
            catalog_entries = catalog_candidates(candidate)
        except CatalogError:
            return _secured_json(
                {"status": "catalog_unavailable", "message": "RTSP compatibility catalog is unavailable."},
                status_code=503,
            )
        normalized = [entry.uri for entry in catalog_entries]
        labels = [entry.label for entry in catalog_entries]
        results = _probe_catalog_sources(catalog_entries, username, request.password)
        catalog_entries = catalog_entries[: len(results)]
        normalized = normalized[: len(results)]
        labels = labels[: len(results)]
    else:
        try:
            normalized = [_normalize_rtsp_url(candidate, source.url) for source in request.sources]
        except ValueError as exc:
            return _secured_json({"status": "invalid_source", "message": str(exc)}, status_code=422)
        if len(set(normalized)) != len(normalized):
            return _secured_json(
                {"status": "duplicate_source", "message": "Each RTSP stream URL must be different."},
                status_code=422,
            )
        labels = [source.label for source in request.sources]
        results = _probe_exact_sources(normalized, username, request.password)
    profiles = []
    for position, (label, uri, result) in enumerate(zip(labels, normalized, results), start=1):
        if result.status != "ready":
            continue
        catalog = catalog_entries[position - 1] if automatic else None
        profiles.append(_manual_profile(label, uri, result, position, catalog))
    if automatic:
        deduplicated: dict[tuple[object, ...], dict[str, object]] = {}
        for profile in profiles:
            media_key = (
                profile.get("encoding"),
                profile.get("width"),
                profile.get("height"),
                round(float(profile.get("fps") or 0), 2),
            )
            deduplicated.setdefault(media_key, profile)
        profiles = list(deduplicated.values())
    rejected = [
        {"position": position, "status": result.status}
        for position, result in enumerate(results, start=1)
        if result.status != "ready"
    ]
    if not profiles:
        authentication_failed = any(result.status == "auth_failed" for result in results)
        return _secured_json(
            {
                "status": "credentials_required" if authentication_failed else "media_unavailable",
                "message": (
                    "Incorrect username or password. Check both fields and try again."
                    if authentication_failed
                    else "No compatible stream was found. Enter an exact RTSP URL and try again."
                    if automatic
                    else "No video could be read. Check the stream path and credentials, then try again."
                ),
                "sources": rejected,
            },
            status_code=401 if authentication_failed else 422,
        )
    roles = select_stream_roles(profiles)
    adopted_candidate = dict(candidate)
    adopted_candidate["display_name"] = request.display_name.strip() or candidate.get("display_name") or candidate.get("ip")
    adoption = repository.adopt(
        adopted_candidate,
        username,
        request.password,
        profiles,
        roles,
    )
    _reconcile_media()
    _queue_frigate_reconciliation()
    adoption = repository.adoption_for_candidate(candidate_uuid) or adoption
    return _secured_json(
        {
            "status": "adopted",
            "profiles": profiles,
            "adoption": adoption,
            "role_tokens": roles,
            "rejected_sources": rejected,
            "catalog_revision": next(
                (profile.get("catalog_revision") for profile in profiles if profile.get("catalog_revision")),
                None,
            ),
        }
    )


@app.get("/healthz", include_in_schema=False)
def health() -> JSONResponse:
    state = snapshot()
    code = 200 if state["status"] == "healthy" else 503
    return _secured_json(state, status_code=code)
