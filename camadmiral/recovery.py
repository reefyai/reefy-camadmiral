from __future__ import annotations

import json
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .media import ProbeResult, probe_source, probe_streams, replace_streams
from .onvif_client import OnvifInspectionError, inspect_onvif_candidate


RECOVERY_RETRY_INTERVAL = max(
    60.0,
    float(os.environ.get("CAMADMIRAL_RECOVERY_RETRY_INTERVAL", "300")),
)
ATTEMPTED_AT: dict[tuple[str, str], float] = {}
ATTEMPT_COUNTS: dict[tuple[str, str], int] = {}
RECOVERY_FAST_RETRY_INTERVAL = 10.0
RECOVERY_FAST_RETRIES = 4


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
) -> tuple[dict[str, str], str]:
    streams = adoption.get("streams", [])
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
        if not tokens or not tokens.issubset(discovered):
            return {}, "profiles_changed"
        updates = {token: discovered[token] for token in tokens}
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
        }
        for future in as_completed(pending):
            token = pending[future]
            try:
                result = future.result()
            except Exception:
                return {}, "error"
            if result.status != "ready":
                return {}, result.status
            if results_by_token is not None:
                results_by_token[token] = result
    return updates, "ready"


def recover_inventory_addresses(repository: Any, inventory_path: Path) -> list[RecoveryResult]:
    inventory = _read_inventory(inventory_path)
    results: list[RecoveryResult] = []
    plans: list[_RecoveryPlan] = []
    adoptions = repository.adoption_map()
    active_camera_uuids = {
        str(adoption["camera_uuid"]) for adoption in adoptions.values()
    }
    for attempt_key in list(ATTEMPTED_AT):
        if attempt_key[0] not in active_camera_uuids:
            ATTEMPTED_AT.pop(attempt_key, None)
            ATTEMPT_COUNTS.pop(attempt_key, None)
    for candidate_uuid, adoption in adoptions.items():
        candidate = inventory.get(candidate_uuid)
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
            if stream.get("uri")
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
        validation_results: dict[str, ProbeResult] = {}
        updates, validation_status = _validated_updates(
            candidate,
            adoption,
            credentials[0],
            credentials[1],
            results_by_token=validation_results,
        )
        if validation_status != "ready":
            results.append(
                RecoveryResult(adoption["camera_uuid"], validation_status, previous_address, address)
            )
            continue
        plans.append(
            _RecoveryPlan(
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
        replace_streams(runtime_sources)
        try:
            repository.enqueue_relay_restart_notification(
                reason="camera_address_recovery",
                camera_count=len(plans),
            )
        except Exception:
            pass
        downstream_results = probe_streams(
            runtime_sources,
            "camadmiral",
            repository.rtsp_access_password(),
        )
        expected_stream_uuids = {
            str(source["stream_uuid"]) for source in runtime_sources
        }
        if set(downstream_results) != expected_stream_uuids or any(
            result.status != "ready" for result in downstream_results.values()
        ):
            raise RuntimeError("restarted relay did not serve every recovered stream")
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
        repository.record_probe_results(empirical_results)
    except Exception:
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
            except Exception:
                pass
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
        repository.resolve_camera_address_incident(address_incidents[plan.camera_uuid])
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
