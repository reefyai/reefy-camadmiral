from __future__ import annotations

from typing import Any


def preserve_inventory(
    state: dict[str, Any],
    inventory: dict[str, Any],
) -> dict[str, Any]:
    if state.get("status") not in {"queued", "running"} or state.get("devices"):
        return state
    merged = dict(state)
    merged["inventory_scan_id"] = inventory.get("scan_id")
    merged["devices"] = inventory.get("devices", [])
    merged["summary"] = inventory.get("summary", {})
    merged["network"] = merged.get("network") or inventory.get("network")
    merged["networks"] = merged.get("networks") or inventory.get("networks", [])
    merged["duration_ms"] = inventory.get("duration_ms")
    merged["completed_at"] = inventory.get("completed_at")
    merged["raw_log"] = inventory.get("raw_log", [])
    return merged
