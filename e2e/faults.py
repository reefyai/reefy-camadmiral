from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, "/opt/camadmiral")

from camadmiral.config import database_path
from camadmiral.crypto import load_master_key
from camadmiral.media import authenticated_rtsp_uri
from camadmiral.storage import CameraRepository


GO2RTC_URL = "http://127.0.0.1:1984"
INVENTORY_PATH = Path("/var/lib/camadmiral/inventory.json")
DATABASE_PATH = Path("/var/lib/camadmiral/camadmiral.db")
GO2RTC_CONFIG_PATH = Path("/run/camadmiral/go2rtc.yaml")
SCAN_STATE_PATH = Path("/run/camadmiral/scan-state.json")
PID_PRESSURE_PREFIX = "synthetic-pid-pressure-"
IDENTITY_CONSUMER_PREFIX = "CamAdmiral-E2E-Identity/"
CONTROL_CONSUMER_PREFIX = "CamAdmiral-E2E-Control/"
ADMIN_PASSWORD_PATH = Path("/run/secrets/camadmiral_admin_password")
IDENTITY_ONVIF_ENDPOINT = "urn:uuid:synthetic-onvif-camera"


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


def write_scan_state(inventory: dict[str, object]) -> None:
    state = {**inventory, "status": "complete", "phase": "complete"}
    temporary = SCAN_STATE_PATH.with_suffix(".e2e.tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(SCAN_STATE_PATH)


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


def identity_consumer(prefix: str = IDENTITY_CONSUMER_PREFIX) -> None:
    matches = []
    for stream_key, stream in streams().items():
        if not isinstance(stream, dict):
            continue
        for consumer in stream.get("consumers") or []:
            if not isinstance(consumer, dict):
                continue
            user_agent = str(consumer.get("user_agent") or "")
            if user_agent.startswith(prefix):
                consumer_id = consumer.get("id")
                remote_addr = str(consumer.get("remote_addr") or "")
                if not consumer_id or not remote_addr:
                    raise RuntimeError(
                        "Identity E2E consumer has no go2rtc ID or remote address"
                    )
                senders = [
                    {
                        "id": sender.get("id"),
                        "parent": sender.get("parent"),
                        "codec_type": (
                            (sender.get("codec") or {}).get("codec_type")
                            if isinstance(sender.get("codec"), dict)
                            else None
                        ),
                        "packets": int(sender.get("packets") or 0),
                        "bytes": int(sender.get("bytes") or 0),
                    }
                    for sender in consumer.get("senders") or []
                    if isinstance(sender, dict) and sender.get("id")
                ]
                producers = []
                for producer in stream.get("producers") or []:
                    if not isinstance(producer, dict):
                        continue
                    raw_url = str(producer.get("url") or "")
                    try:
                        parsed_url = urllib.parse.urlsplit(raw_url)
                        url_host = parsed_url.hostname or ""
                        url_path = parsed_url.path
                    except ValueError:
                        url_host = ""
                        url_path = ""
                    raw_remote = str(producer.get("remote_addr") or "")
                    try:
                        remote_host = (
                            urllib.parse.urlsplit(f"//{raw_remote}").hostname or ""
                        )
                    except ValueError:
                        remote_host = ""
                    receivers = [
                        {
                            "id": receiver.get("id"),
                            "children": sorted(receiver.get("childs") or []),
                            "packets": int(receiver.get("packets") or 0),
                            "bytes": int(receiver.get("bytes") or 0),
                        }
                        for receiver in producer.get("receivers") or []
                        if isinstance(receiver, dict) and receiver.get("id")
                    ]
                    producers.append(
                        {
                            "id": producer.get("id"),
                            "url_host": url_host,
                            "url_path": url_path,
                            "remote_host": remote_host,
                            "bytes_received": int(producer.get("bytes_recv") or 0),
                            "receivers": receivers,
                        }
                    )
                go2rtc_pids = subprocess.check_output(
                    ["pidof", "go2rtc"], text=True, timeout=2
                ).split()
                if len(go2rtc_pids) != 1 or not senders or len(producers) != 1:
                    raise RuntimeError(
                        "Identity E2E consumer has an incomplete relay topology"
                    )
                matches.append(
                    {
                        "stream_key": str(stream_key),
                        "go2rtc_pid": int(go2rtc_pids[0]),
                        "id": consumer_id,
                        "remote_addr": remote_addr,
                        "user_agent": user_agent,
                        "bytes_sent": int(consumer.get("bytes_send") or 0),
                        "senders": senders,
                        "producer": producers[0],
                    }
                )
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one {prefix} E2E consumer, found {len(matches)}: {matches}"
        )
    print(json.dumps(matches[0], sort_keys=True))


def frigate_identity_consumers() -> None:
    runtime = streams()
    with sqlite3.connect(DATABASE_PATH) as connection:
        row = connection.execute(
            "SELECT s.stream_key FROM camera_identity_periods i "
            "JOIN managed_streams s USING(camera_uuid) "
            "JOIN consumer_bindings b USING(stream_uuid) "
            "WHERE i.onvif_identity = ? AND i.ended_at IS NULL "
            "AND b.role = 'detect' ORDER BY s.stream_key LIMIT 1",
            (IDENTITY_ONVIF_ENDPOINT,),
        ).fetchone()
    stream_key = str(row[0]) if row is not None else ""
    stream = runtime.get(stream_key) if stream_key else None
    matches = []
    for consumer in (stream.get("consumers") or []) if isinstance(stream, dict) else []:
        if not isinstance(consumer, dict):
            continue
        remote_addr = str(consumer.get("remote_addr") or "")
        try:
            remote_host = urllib.parse.urlsplit(f"//{remote_addr}").hostname or ""
        except ValueError:
            remote_host = ""
        if remote_host != "172.30.0.30":
            continue
        matches.append(
            {
                "id": consumer.get("id"),
                "remote_addr": remote_addr,
                "user_agent": consumer.get("user_agent"),
            }
        )
    if not stream_key or not matches or any(not item["id"] for item in matches):
        raise RuntimeError(
            "Expected Frigate to consume the identity recovery stream: "
            f"stream={stream_key!r}, consumers={matches}"
        )
    print(
        json.dumps(
            {"stream_key": stream_key, "consumers": matches},
            sort_keys=True,
        )
    )


def identity_diagnostics() -> None:
    diagnostics = []
    for stream_key, stream in streams().items():
        if not isinstance(stream, dict):
            continue
        consumers = []
        for consumer in stream.get("consumers") or []:
            if not isinstance(consumer, dict):
                continue
            user_agent = str(consumer.get("user_agent") or "")
            if not user_agent.startswith(IDENTITY_CONSUMER_PREFIX):
                continue
            consumers.append(
                {
                    "id": consumer.get("id"),
                    "remote_addr": consumer.get("remote_addr"),
                    "user_agent": user_agent,
                    "sender_ids": [
                        sender.get("id")
                        for sender in consumer.get("senders") or []
                        if isinstance(sender, dict)
                    ],
                }
            )
        producers = []
        for producer in stream.get("producers") or []:
            if not isinstance(producer, dict):
                continue
            raw_url = str(producer.get("url") or "")
            raw_remote = str(producer.get("remote_addr") or "")
            try:
                parsed_url = urllib.parse.urlsplit(raw_url)
                url_host = parsed_url.hostname or ""
                url_path = parsed_url.path
            except ValueError:
                url_host = ""
                url_path = ""
            try:
                remote_host = urllib.parse.urlsplit(f"//{raw_remote}").hostname or ""
            except ValueError:
                remote_host = ""
            producers.append(
                {
                    "id": producer.get("id"),
                    "url_host": url_host,
                    "url_path": url_path,
                    "remote_host": remote_host,
                    "receiver_ids": [
                        receiver.get("id")
                        for receiver in producer.get("receivers") or []
                        if isinstance(receiver, dict)
                    ],
                }
            )
        if consumers or str(stream_key).startswith("stream_"):
            diagnostics.append(
                {
                    "stream_key": str(stream_key),
                    "producers": producers,
                    "identity_consumers": consumers,
                }
            )
    print(json.dumps(diagnostics, sort_keys=True))


def assert_open_camera_stale_scan_summary() -> None:
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
    write_scan_state(inventory)

    password = ADMIN_PASSWORD_PATH.read_text(encoding="utf-8").strip()
    credentials = base64.b64encode(f"admin:{password}".encode()).decode()
    request = urllib.request.Request(
        "http://127.0.0.1:18080/internal/discovery",
        headers={"Authorization": f"Basic {credentials}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        decorated = json.load(response)
    devices = decorated.get("devices") or []
    open_device = next(
        (
            device
            for device in devices
            if device.get("candidate_uuid") == "candidate-open"
        ),
        None,
    )
    streams = ((open_device or {}).get("adoption") or {}).get("streams") or []
    summary = decorated.get("summary") or {}
    expected_online = sum(
        device.get("connectivity_status") == "online" for device in devices
    )
    if (
        open_device is None
        or open_device.get("status") != "offline"
        or open_device.get("connectivity_status") != "online"
        or not streams
        or not all(stream.get("health_status") == "healthy" for stream in streams)
        or summary.get("online") != expected_online
        or summary.get("offline") != 0
    ):
        raise RuntimeError(
            "Healthy media did not override the synthetic stale scan state: "
            + json.dumps(
                {
                    "device": open_device,
                    "summary": summary,
                    "expected_online": expected_online,
                },
                sort_keys=True,
            )
        )
    print("healthy media overrode a deliberately stale offline scan observation")


def set_open_camera_observation(address: str) -> None:
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    camera = next(
        (
            device
            for device in inventory.get("devices") or []
            if device.get("candidate_uuid") == "candidate-open"
        ),
        None,
    )
    if camera is None:
        raise RuntimeError("Synthetic open camera is missing from inventory")
    camera.update(
        {
            "ip": address,
            "mac": "02:00:00:00:00:10",
            "status": "online",
            "rtsp": [{"url": f"rtsp://{address}:8554", "status": "available"}],
        }
    )
    write_inventory(inventory)
    print(f"set synthetic open camera observation to {address}")


def assert_onvif_runtime_config_moved() -> None:
    configuration = yaml.safe_load(GO2RTC_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    streams = configuration.get("streams") or {}
    if not isinstance(streams, dict):
        raise RuntimeError("go2rtc runtime config has no stream mapping")
    with sqlite3.connect(DATABASE_PATH) as connection:
        row = connection.execute(
            "SELECT camera_uuid FROM camera_identity_periods "
            "WHERE onvif_identity = ? AND ended_at IS NULL",
            ("urn:uuid:synthetic-onvif-camera",),
        ).fetchone()
        control_row = connection.execute(
            "SELECT camera_uuid FROM cameras WHERE candidate_uuid = ?",
            ("candidate-open",),
        ).fetchone()
    if row is None:
        raise RuntimeError("Database has no current moved ONVIF camera identity")
    if control_row is None:
        raise RuntimeError("Database has no second moved camera")
    repository = CameraRepository(database_path(), load_master_key())
    expected = {
        source["stream_key"]: [
            authenticated_rtsp_uri(
                source["uri"],
                source["username"],
                source["password"],
            )
        ]
        for source in repository.managed_stream_sources(camera_uuid=str(row[0]))
    }
    expected_paths = {
        urllib.parse.urlsplit(value[0]).path for value in expected.values()
    }
    if expected_paths != {"/main", "/sub"}:
        raise RuntimeError("Database has unexpected moved ONVIF stream identities")
    configured = {stream_key: streams.get(stream_key) for stream_key in expected}
    if configured != expected:
        raise RuntimeError("go2rtc runtime config differs from moved database sources")
    control_expected = {
        source["stream_key"]: [
            authenticated_rtsp_uri(
                source["uri"],
                source["username"],
                source["password"],
            )
        ]
        for source in repository.managed_stream_sources(camera_uuid=str(control_row[0]))
    }
    control_configured = {
        stream_key: streams.get(stream_key) for stream_key in control_expected
    }
    if not control_expected or control_configured != control_expected:
        raise RuntimeError("go2rtc runtime config differs from second moved camera sources")
    sources = [
        str(source)
        for value in streams.values()
        for source in (value if isinstance(value, list) else [value])
    ]
    moved = [
        source
        for source in sources
        if urllib.parse.urlsplit(source).hostname == "172.30.0.15"
    ]
    stale = [
        source
        for source in sources
        if urllib.parse.urlsplit(source).hostname == "172.30.0.13"
    ]
    control_moved = [
        source
        for source in sources
        if urllib.parse.urlsplit(source).hostname == "172.30.0.17"
    ]
    control_stale = [
        source
        for source in sources
        if urllib.parse.urlsplit(source).hostname == "172.30.0.12"
    ]
    moved_paths = {urllib.parse.urlsplit(source).path for source in moved}
    control_paths = {urllib.parse.urlsplit(source).path for source in control_moved}
    if (
        moved_paths != {"/main", "/sub"}
        or control_paths != {"/main", "/sub"}
        or stale
        or control_stale
    ):
        raise RuntimeError(
            "go2rtc runtime config did not persist both moved cameras"
        )
    print("go2rtc runtime config contains only the two cameras' moved sources")


def main() -> int:
    action = sys.argv[1:]
    if action == ["delete-managed-stream"]:
        delete_managed_stream()
    elif action == ["identity-consumer"]:
        identity_consumer()
    elif action == ["identity-control-consumer"]:
        identity_consumer(CONTROL_CONSUMER_PREFIX)
    elif action == ["frigate-identity-consumers"]:
        frigate_identity_consumers()
    elif action == ["identity-diagnostics"]:
        identity_diagnostics()
    elif action == ["assert-open-camera-stale-scan-summary"]:
        assert_open_camera_stale_scan_summary()
    elif action == ["set-open-camera-invalid-address"]:
        set_open_camera_observation("172.30.0.99")
    elif action == ["set-open-camera-moved-address"]:
        set_open_camera_observation("172.30.0.12")
    elif action == ["assert-onvif-runtime-config-moved"]:
        assert_onvif_runtime_config_moved()
    elif action == ["seed-scan-pid-pressure"]:
        seed_scan_pid_pressure()
    elif action == ["clear-scan-pid-pressure"]:
        clear_scan_pid_pressure()
    else:
        print(
            "usage: faults.py delete-managed-stream|identity-consumer|"
            "identity-control-consumer|frigate-identity-consumers|"
            "identity-diagnostics|mark-open-camera-scan-offline|"
            "set-open-camera-invalid-address|set-open-camera-moved-address|"
            "assert-onvif-runtime-config-moved|"
            "seed-scan-pid-pressure|clear-scan-pid-pressure",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
