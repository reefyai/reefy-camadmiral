from __future__ import annotations

import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .discovery import LanInterface, scan_explicit_address, scan_lan, scan_targeted_lan
from .inventory import inventory_summary, reconcile_inventory

HEARTBEAT = Path("/run/camadmiral/scanner-heartbeat.json")
REQUEST = Path("/run/camadmiral/scan-request.json")
STATE = Path("/run/camadmiral/scan-state.json")
INVENTORY = settings().storage.inventory
STOP = False


def request_stop(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def read_inventory() -> dict[str, object]:
    try:
        payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"devices": []}
    return payload if isinstance(payload, dict) else {"devices": []}


def write_heartbeat(mode: str = "idle") -> None:
    write_json(
        HEARTBEAT,
        {
            "unix_time": time.time(),
            "pid": os.getpid(),
            "mode": mode,
            "network_access": mode != "idle",
        },
    )


def scan_progress(scanner: str, state: str, interface: LanInterface) -> None:
    write_heartbeat("scanning")
    try:
        current = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        current = {}
    networks = [dict(item) for item in current.get("networks", []) if isinstance(item, dict)]
    subnet = str(interface.network)
    network = next((item for item in networks if item.get("subnet") == subnet), None)
    if network is None:
        network = {**interface.as_dict(), "status": "queued", "scanners": {}}
        networks.append(network)
    else:
        network.update(interface.as_dict())
    network_scanners = dict(network.get("scanners", {}))
    network_scanners[scanner] = state
    network["scanners"] = network_scanners
    scanner_states = list(network_scanners.values())
    if any(value == "running" for value in scanner_states):
        network["status"] = "running"
    elif any(value == "complete" for value in scanner_states):
        network["status"] = "complete"
    elif scanner_states and all(value == "skipped" for value in scanner_states):
        network["status"] = "complete"
    else:
        network["status"] = "error"

    scanners: dict[str, str] = {}
    scanner_names = {
        name
        for item in networks
        for name in dict(item.get("scanners", {}))
    }
    for name in scanner_names:
        states = [
            dict(item.get("scanners", {})).get(name)
            for item in networks
            if name in dict(item.get("scanners", {}))
        ]
        if "running" in states:
            scanners[name] = "running"
        elif "complete" in states:
            scanners[name] = "complete"
        elif states and all(value == "skipped" for value in states):
            scanners[name] = "skipped"
        else:
            scanners[name] = "error"
    current.update(
        {
            "status": "running",
            "phase": "scanning",
            "network": interface.as_dict(),
            "networks": networks,
            "scanners": scanners,
        }
    )
    write_json(STATE, current)


def handle_scan(request: dict[str, object]) -> None:
    scan_id = str(request.get("scan_id", "unknown"))
    previous_inventory = read_inventory()
    previous_devices = previous_inventory.get("devices", [])
    requested_subnets = [
        str(value)
        for value in request.get("subnets", [])
        if isinstance(value, str)
    ]
    write_json(
        STATE,
        {
            "status": "running",
            "phase": "starting",
            "scan_id": scan_id,
            "requested_at": request.get("requested_at"),
            "inventory_scan_id": previous_inventory.get("scan_id"),
            "devices": previous_devices,
            "summary": previous_inventory.get("summary")
            or inventory_summary(previous_devices),
            "network": previous_inventory.get("network"),
            "networks": [
                {"subnet": subnet, "status": "queued", "scanners": {}}
                for subnet in requested_subnets
            ],
            "duration_ms": previous_inventory.get("duration_ms"),
            "completed_at": previous_inventory.get("completed_at"),
            "raw_log": previous_inventory.get("raw_log", []),
        },
    )
    try:
        if request.get("mode") == "address":
            result = scan_explicit_address(str(request.get("address") or ""), progress=scan_progress)
            observations = result["devices"]
            address = str(request.get("address") or "")
            observed_keys = {
                str(observation.get("mac") or "").lower()
                for observation in observations
                if observation.get("mac")
            } | {
                str((observation.get("onvif") or {}).get("endpoint_reference") or "").lower()
                for observation in observations
                if (observation.get("onvif") or {}).get("endpoint_reference")
            }
            target_previous = [
                device
                for device in previous_devices
                if str(device.get("ip") or "") == address
                or str(device.get("mac") or "").lower() in observed_keys
                or str((device.get("onvif") or {}).get("endpoint_reference") or "").lower()
                in observed_keys
            ]
            target_ids = {str(device.get("candidate_uuid")) for device in target_previous}
            untouched = [
                device
                for device in previous_devices
                if str(device.get("candidate_uuid")) not in target_ids
            ]
            updated = reconcile_inventory(
                target_previous,
                observations,
                str(result["completed_at"]),
            )
            result["devices"] = [*untouched, *updated]
        elif request.get("mode") == "targeted":
            target_ids = {
                str(target.get("candidate_uuid"))
                for target in request.get("targets", [])
                if isinstance(target, dict) and target.get("candidate_uuid")
            }
            target_previous = [
                device
                for device in previous_devices
                if str(device.get("candidate_uuid")) in target_ids
            ]
            untouched = [
                device
                for device in previous_devices
                if str(device.get("candidate_uuid")) not in target_ids
            ]
            result = scan_targeted_lan(request.get("targets", []), progress=scan_progress)
            recovered = reconcile_inventory(
                target_previous,
                result["devices"],
                str(result["completed_at"]),
            )
            result["devices"] = sorted(
                [*untouched, *recovered],
                key=lambda device: (
                    device.get("status") != "online",
                    str(device.get("display_name") or device.get("ip") or "").lower(),
                ),
            )
        else:
            result = scan_lan(
                progress=scan_progress,
                known_devices=previous_devices,
                subnets=requested_subnets if "subnets" in request else None,
            )
            result["devices"] = reconcile_inventory(
                previous_devices,
                result["devices"],
                str(result["completed_at"]),
                reachable_ips=result.get("reachable_known", []),
            )
        result["summary"] = inventory_summary(result["devices"])
        result.update(
            {
                "status": "complete",
                "phase": "complete",
                "scan_id": scan_id,
                "inventory_scan_id": scan_id,
            }
        )
        write_json(INVENTORY, result)
        write_json(STATE, result)
    except Exception as exc:
        raw_log = getattr(exc, "raw_log", [])
        networks = getattr(exc, "networks", [])
        write_json(
            STATE,
            {
                "status": "error",
                "phase": "error",
                "scan_id": scan_id,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc)[:300],
                "inventory_scan_id": previous_inventory.get("scan_id"),
                "devices": previous_devices,
                "summary": previous_inventory.get("summary")
                or inventory_summary(previous_devices),
                "network": previous_inventory.get("network"),
                "networks": networks or [
                    {"subnet": subnet, "status": "error", "scanners": {}}
                    for subnet in requested_subnets
                ],
                "raw_log": raw_log or previous_inventory.get("raw_log", []),
            },
        )
    finally:
        REQUEST.unlink(missing_ok=True)
        write_heartbeat()


def main() -> None:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    if not STATE.exists():
        inventory = read_inventory()
        if inventory.get("devices"):
            inventory.update({"status": "complete", "phase": "complete"})
            write_json(STATE, inventory)
        else:
            write_json(STATE, {"status": "idle", "phase": "idle", "devices": []})
    last_heartbeat = 0.0
    while not STOP:
        now = time.monotonic()
        if now - last_heartbeat >= 2:
            write_heartbeat()
            last_heartbeat = now
        if REQUEST.exists():
            try:
                request = json.loads(REQUEST.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                REQUEST.unlink(missing_ok=True)
            else:
                handle_scan(request)
        time.sleep(0.2)


if __name__ == "__main__":
    main()
