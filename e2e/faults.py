from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path


GO2RTC_URL = "http://127.0.0.1:1984"
INVENTORY_PATH = Path("/var/lib/camadmiral/inventory.json")
PID_PRESSURE_PREFIX = "synthetic-pid-pressure-"


def write_inventory(inventory: dict[str, object]) -> None:
    devices = inventory.get("devices") or []
    inventory["summary"] = {
        **(inventory.get("summary") or {}),
        "devices": len(devices),
        "online": sum(device.get("status") == "online" for device in devices),
        "offline": sum(device.get("status") == "offline" for device in devices),
    }
    temporary = INVENTORY_PATH.with_suffix(".e2e.tmp")
    temporary.write_text(json.dumps(inventory), encoding="utf-8")
    temporary.replace(INVENTORY_PATH)


def seed_scan_pid_pressure() -> None:
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    existing = [
        device
        for device in inventory.get("devices") or []
        if not str(device.get("candidate_uuid") or "").startswith(PID_PRESSURE_PREFIX)
    ]
    synthetic = []
    for network_index, network_prefix in enumerate(("172.30.0", "172.31.0")):
        for host in range(100, 132):
            synthetic.append(
                {
                    "candidate_uuid": f"{PID_PRESSURE_PREFIX}{network_index}-{host}",
                    "display_name": f"Synthetic PID pressure {network_index}-{host}",
                    "ip": f"{network_prefix}.{host}",
                    "mac": f"02:ee:{network_index:02x}:00:00:{host:02x}",
                    "status": "online",
                }
            )
    inventory["devices"] = [*existing, *synthetic]
    write_inventory(inventory)
    print(f"seeded {len(synthetic)} synthetic known-camera addresses")


def clear_scan_pid_pressure() -> None:
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    devices = inventory.get("devices") or []
    inventory["devices"] = [
        device
        for device in devices
        if not str(device.get("candidate_uuid") or "").startswith(PID_PRESSURE_PREFIX)
    ]
    removed = len(devices) - len(inventory["devices"])
    write_inventory(inventory)
    print(f"removed {removed} synthetic known-camera addresses")


def streams() -> dict[str, object]:
    with urllib.request.urlopen(f"{GO2RTC_URL}/api/streams", timeout=3) as response:
        state = json.load(response)
    if not isinstance(state, dict):
        raise RuntimeError("go2rtc returned invalid stream state")
    return state


def delete_managed_stream() -> None:
    managed = sorted(name for name in streams() if str(name).startswith("stream_"))
    if not managed:
        raise RuntimeError("No managed stream is available for drift injection")
    stream_key = str(managed[0])
    query = urllib.parse.urlencode({"src": stream_key})
    request = urllib.request.Request(
        f"{GO2RTC_URL}/api/streams?{query}",
        method="DELETE",
    )
    with urllib.request.urlopen(request, timeout=3):
        pass
    if stream_key in streams():
        raise RuntimeError("Managed stream deletion did not take effect")
    print(f"deleted managed stream: {stream_key}")


def mark_open_camera_scan_offline() -> None:
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    devices = inventory.get("devices") or []
    camera = next(
        (
            device
            for device in devices
            if device.get("candidate_uuid") == "candidate-open"
        ),
        None,
    )
    if camera is None:
        raise RuntimeError("Synthetic open camera is missing from inventory")
    camera["status"] = "offline"
    write_inventory(inventory)
    print("marked synthetic open camera offline in stale scan inventory")


def main() -> int:
    action = sys.argv[1:]
    if action == ["delete-managed-stream"]:
        delete_managed_stream()
    elif action == ["mark-open-camera-scan-offline"]:
        mark_open_camera_scan_offline()
    elif action == ["seed-scan-pid-pressure"]:
        seed_scan_pid_pressure()
    elif action == ["clear-scan-pid-pressure"]:
        clear_scan_pid_pressure()
    else:
        print(
            "usage: faults.py delete-managed-stream|mark-open-camera-scan-offline|"
            "seed-scan-pid-pressure|clear-scan-pid-pressure",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
