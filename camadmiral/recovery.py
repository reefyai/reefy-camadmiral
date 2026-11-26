from __future__ import annotations

import json
import os
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .media import ProbeResult, probe_source, probe_streams, replace_streams
from .onvif_client import OnvifInspectionError, inspect_onvif_candidate
from .camera_identity import linked_candidate


RECOVERY_RETRY_INTERVAL = max(
    60.0,
    float(os.environ.get("CAMADMIRAL_RECOVERY_RETRY_INTERVAL", "300")),
)
ATTEMPTED_AT: dict[tuple[str, str], float] = {}
ATTEMPT_COUNTS: dict[tuple[str, str], int] = {}
RECOVERY_FAST_RETRY_INTERVAL = 10.0
RECOVERY_FAST_RETRIES = 4


# Retry transient relay startup failures without resetting the shared process.
# Every ffprobe still uses the existing bounded media-probe timeout.
RELAY_VERIFICATION_ATTEMPTS = 3
RELAY_VERIFICATION_RETRY_DELAY = 1.0
TRANSIENT_PROBE_STATUSES = frozenset({"timeout", "unavailable", "error"})
SAFE_PROBE_STATUSES = TRANSIENT_PROBE_STATUSES | {
    "ready", "auth_failed", "no_video", "invalid_media", "idle",
}


def _diagnostic(event: str, **fields: Any) -> None:
    # Callers supply only generated IDs, enums and numbers, never source URLs,
    # profile tokens, addresses, credentials or exception messages.
    print("media-recovery: " + json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def _probe_diagnostic(stage: str, attempt_id: str, camera_uuid: str,
                      stream_uuid: str, attempt: int, result: ProbeResult) -> None:
    _diagnostic("probe", stage=stage, attempt_id=attempt_id,
                camera_uuid=camera_uuid, stream_uuid=stream_uuid, attempt=attempt,
                status=result.status if result.status in SAFE_PROBE_STATUSES else "error",
                latency_ms=result.latency_ms)


def _verify_recovered_streams(sources: list[dict[str, str]], username: str,
                              password: str, attempt_id: str) -> None:
    expected = {str(source["stream_uuid"]): source for source in sources}
    pending = dict(expected)
    for attempt in range(1, RELAY_VERIFICATION_ATTEMPTS + 1):
        observed = probe_streams(list(pending.values()), username, password)
        if not set(observed).issubset(pending):
            _diagnostic("verification_failed", stage="relay", attempt_id=attempt_id,
                        attempt=attempt, status="unexpected_results")
            raise RuntimeError("Relay verification returned unexpected streams")
        permanent_failure = False
        for stream_uuid, source in list(pending.items()):
            result = observed.get(stream_uuid, ProbeResult("error", 0))
            _probe_diagnostic("relay", attempt_id, str(source["camera_uuid"]),
                              stream_uuid, attempt, result)
            if result.status == "ready":
                pending.pop(stream_uuid)
            elif result.status not in TRANSIENT_PROBE_STATUSES:
                permanent_failure = True
        if not pending:
            return
        if permanent_failure or attempt == RELAY_VERIFICATION_ATTEMPTS:
            break
        _diagnostic("verification_retry", stage="relay", attempt_id=attempt_id,
                    attempt=attempt, pending_streams=len(pending),
                    delay_seconds=RELAY_VERIFICATION_RETRY_DELAY)
        time.sleep(RELAY_VERIFICATION_RETRY_DELAY)
    raise RuntimeError("Relay did not serve every recovered stream within the retry budget")


def _clear_attempts(camera_uuid: str) -> None:
    for attempt_key in list(ATTEMPTED_AT):
        if attempt_key[0] == camera_uuid:
            ATTEMPTED_AT.pop(attempt_key, None)
            ATTEMPT_COUNTS.pop(attempt_key, None)


@dataclass(frozen=True)
class RecoveryResult:
    camera_uuid: str
    status: str
    previous_address: str
    current_address: str


@dataclass(frozen=True)
class _RecoveryPlan:
    attempt_id: str
    candidate_uuid: str
    camera_uuid: str
    candidate: dict[str, Any]
    streams: list[dict[str, Any]]
    previous_address: str
    current_address: str
    evidence: str
    updates: dict[str, str]
    previous_sources: dict[str, str]
    probe_results: dict[str, ProbeResult]


def _read_inventory(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return {
        str(device.get("candidate_uuid")): device
        for device in payload.get("devices", [])
        if device.get("candidate_uuid")
    }


def _source_host(uri: str) -> str:
    try:
        return str(urllib.parse.urlsplit(uri).hostname or "")
    except ValueError:
        return ""


def _replace_host(uri: str, address: str) -> str:
    parsed = urllib.parse.urlsplit(uri)
    host = f"[{address}]" if ":" in address else address
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit(parsed._replace(netloc=host, fragment=""))


def _evidence(candidate: dict[str, Any]) -> str | None:
    onvif = candidate.get("onvif") or {}
    if onvif.get("endpoint_reference"):
        return "onvif-endpoint"
    if candidate.get("mac"):
        return "unique-mac"
    return None


def _validated_updates(
    candidate: dict[str, Any],
    adoption: dict[str, Any],
    username: str,
    password: str,
    *,
    results_by_token: dict[str, ProbeResult] | None = None,
    attempt_id: str = "",
) -> tuple[dict[str, str], str]:
    streams = adoption.get("streams", [])
    enabled_tokens = {str(stream["profile_token"]) for stream in streams if stream.get("enabled", True)}
    source_kinds = {str(stream.get("source_kind") or "onvif") for stream in streams}
    address = str(candidate.get("ip") or "")
    if source_kinds == {"onvif"}:
        try:
            inspection = inspect_onvif_candidate(
                candidate,
                username=username if username else None,
                password=password if username else None,
            )
        except OnvifInspectionError as exc:
            return {}, exc.code
        discovered = {
            str(profile.get("token")): str(profile.get("uri") or "")
            for profile in inspection.get("profiles", [])
            if profile.get("token") and profile.get("uri")
        }
        tokens = {str(stream["profile_token"]) for stream in streams}
        if not enabled_tokens or not enabled_tokens.issubset(discovered):
            return {}, "profiles_changed"
        updates = {token: discovered[token] for token in tokens if token in discovered}
        if any(_source_host(uri) != address for uri in updates.values()):
            return {}, "endpoint_mismatch"
    elif source_kinds.issubset({"manual_rtsp", "catalog_rtsp"}):
        updates = {
            str(stream["profile_token"]): _replace_host(str(stream["uri"]), address)
            for stream in streams
            if stream.get("uri")
        }
    else:
        return {}, "mixed_sources"

    with ThreadPoolExecutor(max_workers=max(1, min(4, len(updates)))) as executor:
        pending = {
            executor.submit(probe_source, uri, username, password): token
            for token, uri in updates.items()
            if token in enabled_tokens
        }
        failure_status = None
        stream_ids = {str(stream["profile_token"]): str(stream.get("stream_uuid") or "")
                      for stream in streams}
        for future in as_completed(pending):
            token = pending[future]
            try:
                result = future.result()
            except Exception:
                result = ProbeResult("error", 0)
            _probe_diagnostic("upstream", attempt_id, str(adoption.get("camera_uuid") or ""),
                              stream_ids[token], 1, result)
            if result.status != "ready" and failure_status is None:
                failure_status = result.status
            if results_by_token is not None:
                results_by_token[token] = result
        if failure_status is not None:
            return {}, failure_status
    return updates, "ready"


def recover_inventory_addresses(repository: Any, inventory_path: Path) -> list[RecoveryResult]:
    inventory = _read_inventory(inventory_path)
    results: list[RecoveryResult] = []
    plans: list[_RecoveryPlan] = []
    adoptions = repository.adoption_map()
    identity_links = repository.camera_identity_links()
    active_camera_uuids = {
        str(adoption["camera_uuid"]) for adoption in adoptions.values()
    }
    for attempt_key in list(ATTEMPTED_AT):
        if attempt_key[0] not in active_camera_uuids:
            ATTEMPTED_AT.pop(attempt_key, None)
            ATTEMPT_COUNTS.pop(attempt_key, None)
    for candidate_uuid, adoption in adoptions.items():
        if not adoption.get('enabled', True):
            continue
        candidate = linked_candidate(adoption, inventory, identity_links, candidate_uuid)
        if (
            not candidate
            or candidate.get("status") != "online"
            or candidate.get("identity_conflict")
        ):
            continue
        evidence = _evidence(candidate)
        if evidence is None:
            continue
        address = str(candidate.get("ip") or "")
        streams = adoption.get("streams", [])
        previous_hosts = {
            _source_host(str(stream.get("uri") or ""))
            for stream in streams
            if stream.get("uri") and stream.get("enabled", True)
        }
        previous_hosts.discard("")
        if not address or len(previous_hosts) != 1:
            continue
        camera_uuid = str(adoption["camera_uuid"])
        if previous_hosts == {address}:
            _clear_attempts(camera_uuid)
            continue
        previous_address = next(iter(previous_hosts))
        attempt_key = (camera_uuid, address)
        now = time.monotonic()
        previous_attempt = ATTEMPTED_AT.get(attempt_key)
        attempt_count = ATTEMPT_COUNTS.get(attempt_key, 0)
        retry_interval = (
            RECOVERY_FAST_RETRY_INTERVAL
            if attempt_count <= RECOVERY_FAST_RETRIES
            else RECOVERY_RETRY_INTERVAL
        )
        if previous_attempt is not None and now - previous_attempt < retry_interval:
            continue
        ATTEMPTED_AT[attempt_key] = now
        ATTEMPT_COUNTS[attempt_key] = attempt_count + 1
        credentials = repository.credentials_for_candidate(candidate_uuid)
        if credentials is None:
            continue
        attempt_id = uuid.uuid4().hex
        _diagnostic("validation_started", stage="upstream", attempt_id=attempt_id,
                    camera_uuid=camera_uuid, recovery_attempt=attempt_count + 1,
                    enabled_streams=sum(stream.get("enabled", True) for stream in streams))
        validation_results: dict[str, ProbeResult] = {}
        updates, validation_status = _validated_updates(
            candidate,
            adoption,
            credentials[0],
            credentials[1],
            results_by_token=validation_results,
            attempt_id=attempt_id,
        )
        if validation_status != "ready":
            _diagnostic("validation_failed", stage="upstream", attempt_id=attempt_id,
                        camera_uuid=camera_uuid, status=validation_status
                        if validation_status in SAFE_PROBE_STATUSES | {"profiles_changed", "endpoint_mismatch", "mixed_sources", "unreachable", "credentials_required", "onvif_error", "no_profiles", "unsafe_endpoint", "not_onvif", "invalid_response"} else "error")
            results.append(
                RecoveryResult(adoption["camera_uuid"], validation_status, previous_address, address)
            )
            continue
        plans.append(
            _RecoveryPlan(
                attempt_id=attempt_id,
                candidate_uuid=candidate_uuid,
                camera_uuid=camera_uuid,
                candidate=candidate,
                streams=streams,
                previous_address=previous_address,
                current_address=address,
                evidence=evidence,
                updates=updates,
                previous_sources={
                    str(stream["profile_token"]): str(stream["uri"])
                    for stream in streams
                    if stream.get("uri")
                },
                probe_results=validation_results,
            )
        )

    if not plans:
        return results

    revision_id: int | None = None
    revision_status: str | None = None
    updated_plans: list[_RecoveryPlan] = []
    address_incidents: dict[str, str] = {}
    runtime_attempted = False
    runtime_attempt_id = plans[0].attempt_id if len(plans) == 1 else uuid.uuid4().hex
    _diagnostic("cutover_started", stage="relay_replace", attempt_id=runtime_attempt_id,
                validation_attempt_ids=[plan.attempt_id for plan in plans], camera_count=len(plans))
    stage = "save_sources"
    try:
        for plan in plans:
            address_incidents[plan.camera_uuid] = repository.open_camera_address_incident(
                plan.camera_uuid,
                plan.previous_address,
                plan.current_address,
                plan.evidence,
            )
            repository.update_profile_sources(plan.camera_uuid, plan.updates)
            updated_plans.append(plan)
        changed_camera_uuids = {plan.camera_uuid for plan in plans}
        all_sources = repository.managed_stream_sources()
        runtime_sources = [
            source
            for source in all_sources
            if source["camera_uuid"] in changed_camera_uuids
        ]
        revision_id, revision_status = repository.record_desired_media_revision(all_sources)
        runtime_attempted = True
        stage = "relay_replace"
        replace_streams(runtime_sources)
        try:
            repository.enqueue_relay_restart_notification(
                reason="camera_address_recovery",
                camera_count=len(plans),
            )
        except Exception:
            pass
        stage = "relay_verification"
        _verify_recovered_streams(runtime_sources, "camadmiral",
                                  repository.rtsp_access_password(), runtime_attempt_id)
        empirical_results: dict[str, ProbeResult] = {}
        for plan in plans:
            stream_ids_by_token = {
                str(stream["profile_token"]): str(stream["stream_uuid"])
                for stream in plan.streams
            }
            empirical_results.update(
                {
                    stream_ids_by_token[token]: result
                    for token, result in plan.probe_results.items()
                    if token in stream_ids_by_token
                }
            )
        stage = "commit_health"
        repository.complete_address_recovery(list(address_incidents.values()), empirical_results)
    except Exception as exc:
        _diagnostic("rollback_started", stage=stage, attempt_id=runtime_attempt_id,
                    camera_count=len(plans), error_type=type(exc).__name__)
        if revision_id is not None and revision_status == "desired":
            repository.complete_media_revision(
                revision_id,
                "failed",
                "runtime_replacement_failed",
            )
        for plan in updated_plans:
            repository.update_profile_sources(plan.camera_uuid, plan.previous_sources)
        if runtime_attempted:
            rollback_camera_uuids = {plan.camera_uuid for plan in updated_plans}
            rollback_sources = [
                source
                for source in repository.managed_stream_sources()
                if source["camera_uuid"] in rollback_camera_uuids
            ]
            try:
                replace_streams(rollback_sources)
                repository.enqueue_relay_restart_notification(
                    reason="camera_address_recovery_rollback",
                    camera_count=len(updated_plans),
                )
            except Exception as exc:
                _diagnostic("rollback_failed", stage="rollback", attempt_id=runtime_attempt_id,
                            error_type=type(exc).__name__)
        results.extend(
            RecoveryResult(
                plan.camera_uuid,
                "runtime_failed",
                plan.previous_address,
                plan.current_address,
            )
            for plan in plans
        )
        return results

    if revision_id is not None and revision_status == "desired":
        repository.complete_media_revision(revision_id, "applied")
    for plan in plans:
        repository.record_address_change(
            plan.camera_uuid,
            plan.previous_address,
            plan.current_address,
            plan.evidence,
        )
        repository.observe_camera_identity(plan.camera_uuid, plan.candidate)
        if plan.camera_uuid in identity_links:
            repository.bind_camera_identity(plan.camera_uuid, plan.candidate)
        _diagnostic("recovered", stage="complete", attempt_id=runtime_attempt_id,
                    camera_uuid=plan.camera_uuid)
        _clear_attempts(plan.camera_uuid)
        results.append(
            RecoveryResult(
                plan.camera_uuid,
                "recovered",
                plan.previous_address,
                plan.current_address,
            )
        )
    return results
