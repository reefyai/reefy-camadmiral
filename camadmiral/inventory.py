from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from typing import Any, Iterable


def _stable_keys(device: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    mac = str(device.get("mac") or "").strip().lower()
    if mac:
        keys.append(f"mac:{mac}")
    onvif = device.get("onvif") or {}
    endpoint_reference = str(onvif.get("endpoint_reference") or "").strip().lower()
    if endpoint_reference:
        keys.append(f"onvif:{endpoint_reference}")
    return keys


def annotate_identity_conflicts(devices: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    items = [dict(device) for device in devices]
    owners: dict[str, list[int]] = defaultdict(list)
    for index, device in enumerate(items):
        for key in _stable_keys(device):
            owners[key].append(index)
    conflicts: dict[int, list[str]] = defaultdict(list)
    for key, indices in owners.items():
        if len(indices) > 1:
            label = "MAC address" if key.startswith("mac:") else "ONVIF identity"
            for index in indices:
                conflicts[index].append(label)
    for index, device in enumerate(items):
        labels = sorted(set(conflicts.get(index, [])))
        device["identity_conflict"] = bool(labels)
        if labels:
            device["identity_conflict_reason"] = "Duplicate " + " and ".join(labels)
        else:
            device.pop("identity_conflict_reason", None)
    return items


def _merge_online(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    seen_at: str,
) -> dict[str, Any]:
    merged = {**(previous or {}), **current}
    same_address = bool(previous) and previous.get("ip") == current.get("ip")
    if same_address:
        for field in ("onvif", "rtsp"):
            if not current.get(field) and previous.get(field):
                merged[field] = previous[field]
        if (
            current.get("display_name") == current.get("ip")
            and previous.get("display_name")
        ):
            merged["display_name"] = previous["display_name"]
    merged.update(
        {
            "candidate_uuid": (previous or {}).get("candidate_uuid")
            or current.get("candidate_uuid")
            or str(uuid.uuid4()),
            "status": "online",
            "first_seen": (previous or {}).get("first_seen", seen_at),
            "last_seen": seen_at,
            "last_service_seen": seen_at,
            "service_status": "available",
            "missed_scans": 0,
        }
    )
    return merged


def reconcile_inventory(
    previous_devices: Iterable[dict[str, Any]],
    current_devices: Iterable[dict[str, Any]],
    seen_at: str,
    reachable_ips: Iterable[str] = (),
) -> list[dict[str, Any]]:
    previous = list(previous_devices)
    current = list(current_devices)
    reachable = {str(address) for address in reachable_ips}
    previous_stable_indices: dict[str, list[int]] = defaultdict(list)
    ip_index: dict[str, int] = {}
    for index, device in enumerate(previous):
        for key in _stable_keys(device):
            previous_stable_indices[key].append(index)
        address = str(device.get("ip") or "")
        if address:
            ip_index.setdefault(address, index)
    current_stable_counts = Counter(
        key
        for device in current
        for key in _stable_keys(device)
    )
    stable_index = {
        key: indices[0]
        for key, indices in previous_stable_indices.items()
        if len(indices) == 1 and current_stable_counts[key] == 1
    }

    matched: set[int] = set()
    reconciled: list[dict[str, Any]] = []
    for observation in current:
        current_keys = _stable_keys(observation)
        previous_index = next(
            (
                stable_index[key]
                for key in current_keys
                if key in stable_index and stable_index[key] not in matched
            ),
            None,
        )
        if previous_index is None and not current_keys:
            candidate = ip_index.get(str(observation.get("ip") or ""))
            if candidate is not None and candidate not in matched:
                previous_index = candidate
        old = previous[previous_index] if previous_index is not None else None
        if previous_index is not None:
            matched.add(previous_index)
        reconciled.append(_merge_online(old, observation, seen_at))

    for index, old in enumerate(previous):
        if index in matched:
            continue
        if str(old.get("ip") or "") in reachable:
            network_only = dict(old)
            network_only.update(
                {
                    "candidate_uuid": old.get("candidate_uuid") or str(uuid.uuid4()),
                    "status": "online",
                    "service_status": "unavailable",
                    "last_seen": seen_at,
                    "last_service_seen": old.get("last_service_seen")
                    or old.get("last_seen"),
                    "missed_scans": 0,
                }
            )
            network_only.setdefault("first_seen", old.get("last_seen", seen_at))
            reconciled.append(network_only)
            continue
        offline = dict(old)
        offline.update(
            {
                "candidate_uuid": old.get("candidate_uuid") or str(uuid.uuid4()),
                "status": "offline",
                "missed_scans": int(old.get("missed_scans", 0)) + 1,
            }
        )
        offline.setdefault("first_seen", old.get("last_seen", seen_at))
        offline.setdefault("last_seen", seen_at)
        reconciled.append(offline)

    return sorted(
        annotate_identity_conflicts(reconciled),
        key=lambda device: (
            device.get("status") != "online",
            str(device.get("display_name") or device.get("ip") or "").lower(),
        ),
    )


def inventory_summary(devices: Iterable[dict[str, Any]]) -> dict[str, int]:
    items = list(devices)
    return {
        "devices": len(items),
        "online": sum(device.get("status") == "online" for device in items),
        "offline": sum(device.get("status") == "offline" for device in items),
        "onvif": sum(bool(device.get("onvif")) for device in items),
        "rtsp": sum(bool(device.get("rtsp")) for device in items),
        "services_unavailable": sum(
            device.get("status") == "online"
            and device.get("service_status") == "unavailable"
            for device in items
        ),
        "ignored": sum(bool(device.get("ignored")) for device in items),
        "conflicts": sum(bool(device.get("identity_conflict")) for device in items),
    }
