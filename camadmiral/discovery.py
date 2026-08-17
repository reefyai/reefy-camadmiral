from __future__ import annotations

import concurrent.futures
import fcntl
import ipaddress
import os
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

ROUTE_TABLE = Path("/proc/net/route")
ARP_TABLE = Path("/proc/net/arp")
ONVIF_MULTICAST = ("239.255.255.250", 3702)
RTSP_PORTS = (554, 8554)
MAX_SCAN_HOSTS = int(os.environ.get("CAMADMIRAL_MAX_SCAN_HOSTS", "1024"))
RTSP_WORKERS = int(os.environ.get("CAMADMIRAL_RTSP_WORKERS", "64"))
REACHABILITY_WORKERS = int(os.environ.get("CAMADMIRAL_REACHABILITY_WORKERS", "32"))
RTSP_CONNECT_TIMEOUT = float(os.environ.get("CAMADMIRAL_RTSP_TIMEOUT", "0.4"))
ONVIF_TIMEOUT = float(os.environ.get("CAMADMIRAL_ONVIF_TIMEOUT", "2.5"))
MAX_SCAN_LOG_LINES = 5000


class DiscoveryScanError(RuntimeError):
    def __init__(self, message: str, raw_log: list[str]):
        super().__init__(message)
        self.raw_log = raw_log


@dataclass(frozen=True)
class LanInterface:
    name: str
    address: ipaddress.IPv4Address
    network: ipaddress.IPv4Network

    def as_dict(self) -> dict[str, str | int]:
        return {
            "interface": self.name,
            "address": str(self.address),
            "subnet": str(self.network),
            "hosts": max(0, self.network.num_addresses - 2),
        }


def _interface_ipv4(name: str, request: int) -> ipaddress.IPv4Address:
    packed_name = struct.pack("256s", name.encode("utf-8")[:15])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        result = fcntl.ioctl(sock.fileno(), request, packed_name)
    return ipaddress.IPv4Address(result[20:24])


def default_lan_interface(route_text: str | None = None) -> LanInterface:
    route_text = ROUTE_TABLE.read_text(encoding="utf-8") if route_text is None else route_text
    candidates: list[tuple[int, str]] = []
    for line in route_text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8:
            continue
        name, destination, _gateway, flags, _ref, _use, metric, _mask = fields[:8]
        if destination != "00000000" or not (int(flags, 16) & 0x1):
            continue
        candidates.append((int(metric), name))
    if not candidates:
        raise RuntimeError("No default LAN interface found")

    name = min(candidates)[1]
    address = _interface_ipv4(name, 0x8915)  # SIOCGIFADDR
    netmask = _interface_ipv4(name, 0x891B)  # SIOCGIFNETMASK
    network = ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)
    if not address.is_private:
        raise RuntimeError(f"Default interface {name} is not on a private IPv4 network")
    host_count = max(0, network.num_addresses - 2)
    if host_count > MAX_SCAN_HOSTS:
        raise RuntimeError(
            f"LAN subnet {network} has {host_count} hosts; safety limit is {MAX_SCAN_HOSTS}"
        )
    return LanInterface(name=name, address=address, network=network)


def _probe_message(
    discovery_namespace: str,
    addressing_namespace: str,
    probe_type: str | None = None,
) -> bytes:
    if discovery_namespace.endswith("2009/01/discovery"):
        target = "urn:docs-oasis-open-org:ws-dd:ns:discovery:2009:01"
    else:
        target = "urn:schemas-xmlsoap-org:ws:2005:04:discovery"
    if addressing_namespace.endswith("2004/08/addressing"):
        anonymous = f"{addressing_namespace}/role/anonymous"
    else:
        anonymous = f"{addressing_namespace}/anonymous"
    type_namespaces = (
        ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl"'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
    )
    probe_body = f"<d:Probe><d:Types>{probe_type}</d:Types></d:Probe>" if probe_type else "<d:Probe/>"
    envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:a="{addressing_namespace}" xmlns:d="{discovery_namespace}"{type_namespaces}>
 <s:Header>
  <a:Action s:mustUnderstand="1">{discovery_namespace}/Probe</a:Action>
  <a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>
  <a:ReplyTo><a:Address>{anonymous}</a:Address></a:ReplyTo>
  <a:To s:mustUnderstand="1">{target}</a:To>
 </s:Header>
 <s:Body>{probe_body}</s:Body>
</s:Envelope>"""
    return envelope.encode("utf-8")


def onvif_probe_messages() -> tuple[bytes, ...]:
    return (
        _probe_message(
            "http://schemas.xmlsoap.org/ws/2005/04/discovery",
            "http://schemas.xmlsoap.org/ws/2004/08/addressing",
        ),
        _probe_message(
            "http://schemas.xmlsoap.org/ws/2005/04/discovery",
            "http://schemas.xmlsoap.org/ws/2004/08/addressing",
            "dn:NetworkVideoTransmitter",
        ),
        _probe_message(
            "http://schemas.xmlsoap.org/ws/2005/04/discovery",
            "http://schemas.xmlsoap.org/ws/2004/08/addressing",
            "tds:Device",
        ),
        _probe_message(
            "http://docs.oasis-open.org/ws-dd/ns/discovery/2009/01",
            "http://www.w3.org/2005/08/addressing",
        ),
    )


def is_onvif_camera(match: dict[str, Any]) -> bool:
    types = {str(value).lower() for value in match.get("types", [])}
    if any(
        value.endswith(":networkvideotransmitter") or value == "tds:device"
        for value in types
    ):
        return True
    scopes = [str(value).lower() for value in match.get("scopes", [])]
    if any(value.startswith("onvif://www.onvif.org/") for value in scopes):
        return True
    service_urls = [str(value).lower() for value in match.get("service_urls", [])]
    return any("/onvif/" in value for value in service_urls)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _element_text(element: ET.Element, name: str) -> str:
    for child in element.iter():
        if _local_name(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _scope_metadata(scopes: Iterable[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for scope in scopes:
        parsed = urllib.parse.urlsplit(scope)
        parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            continue
        key = parts[-2].lower()
        value = parts[-1].strip()
        if key in {"name", "hardware"} and value:
            metadata.setdefault(key, value)
    return metadata


def parse_probe_matches(payload: bytes, sender_ip: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return []
    matches: list[dict[str, Any]] = []
    for element in root.iter():
        if _local_name(element.tag) != "ProbeMatch":
            continue
        xaddrs = _element_text(element, "XAddrs").split()
        scopes = [
            scope
            for scope in _element_text(element, "Scopes").split()
            if "/location/" not in scope.lower()
        ]
        types = _element_text(element, "Types").split()
        endpoint_reference = _element_text(element, "Address")
        scope_metadata = _scope_metadata(scopes)
        matches.append(
            {
                "ip": sender_ip,
                "endpoint_reference": endpoint_reference or None,
                "service_urls": sorted(set(xaddrs)),
                "scopes": scopes,
                "types": types,
                "name": scope_metadata.get("name"),
                "model": scope_metadata.get("hardware"),
            }
        )
    return matches


def discover_onvif(
    interface: LanInterface,
    log: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    discovered: dict[str, dict[str, Any]] = {}
    emit = log or (lambda _message: None)
    emit(
        f"ONVIF: bind {interface.address}; multicast {ONVIF_MULTICAST[0]}:{ONVIF_MULTICAST[1]}; "
        f"timeout {ONVIF_TIMEOUT:.1f}s"
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, interface.address.packed)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.bind((str(interface.address), 0))
        messages = onvif_probe_messages()
        for index, message in enumerate(messages, start=1):
            sock.sendto(message, ONVIF_MULTICAST)
            emit(f"ONVIF: sent multicast probe {index}/{len(messages)} ({len(message)} bytes)")
        # A bounded unicast probe helps cameras with broken or disabled multicast.
        for host in interface.network.hosts():
            if host != interface.address:
                sock.sendto(messages[0], (str(host), ONVIF_MULTICAST[1]))
                emit(f"ONVIF: sent unicast probe to {host}:{ONVIF_MULTICAST[1]}")
        deadline = time.monotonic() + ONVIF_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                payload, sender = sock.recvfrom(65535)
            except socket.timeout:
                break
            emit(f"ONVIF: received {len(payload)} bytes from {sender[0]}:{sender[1]}")
            matches = parse_probe_matches(payload, sender[0])
            if not matches:
                emit(f"ONVIF: ignored response from {sender[0]} without a valid ProbeMatch")
            for match in matches:
                if not is_onvif_camera(match):
                    emit(f"ONVIF: ignored non-camera response from {sender[0]}")
                    continue
                emit(
                    f"ONVIF: accepted camera {sender[0]}; endpoint="
                    f"{match.get('endpoint_reference') or '-'}; xaddrs="
                    f"{', '.join(match.get('service_urls', [])) or '-'}"
                )
                key = match.get("endpoint_reference") or match["ip"]
                existing = discovered.get(str(key))
                if existing is None:
                    discovered[str(key)] = match
                    continue
                existing["service_urls"] = sorted(
                    set(existing["service_urls"]) | set(match["service_urls"])
                )
                existing["scopes"] = sorted(set(existing["scopes"]) | set(match["scopes"]))
                existing["types"] = sorted(set(existing["types"]) | set(match["types"]))
    result = sorted(discovered.values(), key=lambda item: ipaddress.ip_address(item["ip"]))
    emit(f"ONVIF: complete; {len(result)} camera(s) found")
    return result


def discover_onvif_address(
    interface: LanInterface,
    address: str,
    log: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Probe one explicitly requested LAN address without widening the scan."""
    emit = log or (lambda _message: None)
    target = ipaddress.IPv4Address(address)
    if target not in interface.network or target == interface.address:
        raise ValueError(f"Address must be inside {interface.network}")
    discovered: dict[str, dict[str, Any]] = {}
    messages = onvif_probe_messages()
    emit(f"ONVIF: probing explicit address {target}:{ONVIF_MULTICAST[1]}")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.bind((str(interface.address), 0))
        for message in messages:
            sock.sendto(message, (str(target), ONVIF_MULTICAST[1]))
        deadline = time.monotonic() + ONVIF_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                payload, sender = sock.recvfrom(65535)
            except socket.timeout:
                break
            if sender[0] != str(target):
                continue
            for match in parse_probe_matches(payload, sender[0]):
                if not is_onvif_camera(match):
                    continue
                key = str(match.get("endpoint_reference") or match["ip"])
                discovered[key] = match
    result = list(discovered.values())
    emit(f"ONVIF: explicit probe found {len(result)} camera service(s)")
    return result


def _probe_rtsp(address: str, port: int) -> dict[str, Any] | None:
    started = time.monotonic()
    try:
        with socket.create_connection((address, port), timeout=RTSP_CONNECT_TIMEOUT) as sock:
            sock.settimeout(RTSP_CONNECT_TIMEOUT)
            request = (
                "OPTIONS * RTSP/1.0\r\n"
                "CSeq: 1\r\n"
                "User-Agent: CamAdmiral/0.1\r\n\r\n"
            ).encode("ascii")
            sock.sendall(request)
            try:
                response = sock.recv(1024)
            except socket.timeout:
                response = b""
    except OSError:
        return None
    first_line = response.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    verified = first_line.startswith("RTSP/")
    return {
        "port": port,
        "url": f"rtsp://{address}:{port}",
        "verified": verified,
        "response": first_line if verified else None,
        "latency_ms": round((time.monotonic() - started) * 1000),
    }


def discover_rtsp(
    interface: LanInterface,
    log: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    emit = log or (lambda _message: None)
    targets = [str(host) for host in interface.network.hosts() if host != interface.address]
    emit(
        f"RTSP: probing {len(targets)} host(s), ports "
        f"{', '.join(str(port) for port in RTSP_PORTS)}, {RTSP_WORKERS} workers, "
        f"timeout {RTSP_CONNECT_TIMEOUT:.1f}s"
    )
    discovered: dict[str, list[dict[str, Any]]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=RTSP_WORKERS) as pool:
        futures = {
            pool.submit(_probe_rtsp, address, port): (address, port)
            for address in targets
            for port in RTSP_PORTS
        }
        for future in concurrent.futures.as_completed(futures):
            address, port = futures[future]
            endpoint = future.result()
            if endpoint is None:
                emit(f"RTSP: {address}:{port} no RTSP response")
                continue
            if not endpoint["verified"]:
                emit(f"RTSP: {address}:{port} TCP open but response was not RTSP")
                continue
            emit(
                f"RTSP: {address}:{port} responded in {endpoint['latency_ms']}ms; "
                f"{endpoint.get('response') or 'TCP open without RTSP status'}"
            )
            discovered.setdefault(address, []).append(endpoint)
    result = [
        {"ip": address, "endpoints": sorted(endpoints, key=lambda item: item["port"])}
        for address, endpoints in sorted(
            discovered.items(), key=lambda item: ipaddress.ip_address(item[0])
        )
    ]
    emit(f"RTSP: complete; {len(result)} host(s) responded")
    return result


def discover_rtsp_address(
    interface: LanInterface,
    address: str,
    log: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    emit = log or (lambda _message: None)
    target = ipaddress.IPv4Address(address)
    if target not in interface.network or target == interface.address:
        raise ValueError(f"Address must be inside {interface.network}")
    endpoints = [
        endpoint
        for port in RTSP_PORTS
        if (endpoint := _probe_rtsp(str(target), port)) is not None
        and endpoint["verified"]
    ]
    emit(f"RTSP: explicit probe found {len(endpoints)} endpoint(s) at {target}")
    return [{"ip": str(target), "endpoints": endpoints}] if endpoints else []


def scan_explicit_address(
    address: str,
    progress: Callable[[str, str, LanInterface], None] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    raw_log: list[str] = []

    def log(message: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        raw_log.append(f"{timestamp} {' '.join(str(message).splitlines())}")

    interface = default_lan_interface()
    target = ipaddress.IPv4Address(address)
    if not target.is_private or target not in interface.network or target == interface.address:
        raise DiscoveryScanError(
            f"Address must be a camera address inside {interface.network}", raw_log
        )
    scanners = {"onvif": "running", "rtsp": "running"}
    if progress:
        for scanner in scanners:
            progress(scanner, "running", interface)
    results: dict[str, Any] = {"onvif": [], "rtsp": []}
    errors: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(discover_onvif_address, interface, str(target), log): "onvif",
            pool.submit(discover_rtsp_address, interface, str(target), log): "rtsp",
        }
        for future in concurrent.futures.as_completed(futures):
            scanner = futures[future]
            try:
                results[scanner] = future.result()
            except Exception as exc:
                scanners[scanner] = "error"
                errors[scanner] = str(exc)[:200]
            else:
                scanners[scanner] = "complete"
            if progress:
                progress(scanner, scanners[scanner], interface)
    arp_entries = read_arp_table()
    devices = merge_discovery(results["onvif"], results["rtsp"], arp_entries)
    duration_ms = round((time.monotonic() - started) * 1000)
    log(f"EXPLICIT: complete in {duration_ms}ms; devices={len(devices)}")
    return {
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "duration_ms": duration_ms,
        "network": interface.as_dict(),
        "scanners": scanners,
        "scanner_errors": errors,
        "devices": devices,
        "raw_log": raw_log,
    }


def _ping_host(address: str) -> bool:
    try:
        result = subprocess.run(
            ["ping", "-n", "-c", "2", "-W", "1", address],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def discover_reachable_known(
    interface: LanInterface,
    known_devices: Iterable[dict[str, Any]],
    log: Callable[[str], None] | None = None,
) -> list[str]:
    emit = log or (lambda _message: None)
    targets: list[str] = []
    for device in known_devices:
        address = str(device.get("ip") or "")
        if not device.get("mac"):
            continue
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if parsed in interface.network and parsed != interface.address:
            targets.append(address)
    targets = sorted(set(targets), key=ipaddress.ip_address)
    emit(f"REACHABILITY: checking {len(targets)} known camera address(es) with two ICMP probes")
    reachable: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=REACHABILITY_WORKERS) as pool:
        futures = {pool.submit(_ping_host, address): address for address in targets}
        for future in concurrent.futures.as_completed(futures):
            address = futures[future]
            if future.result():
                reachable.append(address)
                emit(f"REACHABILITY: {address} answered ICMP")
            else:
                emit(f"REACHABILITY: {address} did not answer ICMP")
    return sorted(reachable, key=ipaddress.ip_address)


def read_arp_table(text: str | None = None) -> dict[str, str]:
    try:
        text = ARP_TABLE.read_text(encoding="utf-8") if text is None else text
    except OSError:
        return {}
    entries: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[2] == "0x0":
            continue
        mac = fields[3].lower()
        if mac != "00:00:00:00:00:00":
            entries[fields[0]] = mac
    return entries


def merge_discovery(
    onvif_devices: Iterable[dict[str, Any]],
    rtsp_devices: Iterable[dict[str, Any]],
    arp_entries: dict[str, str],
) -> list[dict[str, Any]]:
    devices: dict[str, dict[str, Any]] = {}
    for onvif in onvif_devices:
        address = onvif["ip"]
        device = devices.setdefault(address, {"ip": address, "onvif": None, "rtsp": []})
        device["onvif"] = onvif
    for rtsp in rtsp_devices:
        address = rtsp["ip"]
        device = devices.setdefault(address, {"ip": address, "onvif": None, "rtsp": []})
        device["rtsp"] = rtsp["endpoints"]
    for address, device in devices.items():
        device["mac"] = arp_entries.get(address)
        onvif = device["onvif"] or {}
        device["display_name"] = onvif.get("name") or onvif.get("model") or address
    return sorted(devices.values(), key=lambda item: ipaddress.ip_address(item["ip"]))


def discover_targeted(
    interface: LanInterface,
    targets: Iterable[dict[str, Any]],
    log: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    emit = log or (lambda _message: None)
    targets = list(targets)
    if not targets:
        return []
    emit(f"RECOVERY: looking for {len(targets)} known camera identity(s)")
    onvif_devices = discover_onvif(interface, emit)
    arp_entries = read_arp_table()
    onvif_by_endpoint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for device in onvif_devices:
        endpoint = str(device.get("endpoint_reference") or "").lower()
        if endpoint:
            onvif_by_endpoint[endpoint].append(device)
    addresses_by_mac: dict[str, list[str]] = defaultdict(list)
    for address, mac in arp_entries.items():
        addresses_by_mac[str(mac).lower()].append(address)

    matches: dict[str, str] = {}
    for target in targets:
        candidate_uuid = str(target.get("candidate_uuid") or "")
        endpoint = str(target.get("endpoint_reference") or "").lower()
        mac = str(target.get("mac") or "").lower()
        endpoint_matches = onvif_by_endpoint.get(endpoint, []) if endpoint else []
        mac_matches = addresses_by_mac.get(mac, []) if mac else []
        if len(endpoint_matches) == 1:
            matches[candidate_uuid] = str(endpoint_matches[0]["ip"])
            emit(f"RECOVERY: matched {candidate_uuid} by ONVIF endpoint identity")
        elif len(mac_matches) == 1:
            matches[candidate_uuid] = str(mac_matches[0])
            emit(f"RECOVERY: matched {candidate_uuid} by unique local MAC")
        elif len(endpoint_matches) > 1 or len(mac_matches) > 1:
            emit(f"RECOVERY: ignored ambiguous identity for {candidate_uuid}")

    address_counts = Counter(matches.values())
    matched_addresses = {
        address
        for address in matches.values()
        if address_counts[address] == 1
    }
    matched_onvif = [
        device for device in onvif_devices if str(device.get("ip")) in matched_addresses
    ]
    matched_rtsp: list[dict[str, Any]] = []
    for address in sorted(matched_addresses, key=ipaddress.ip_address):
        endpoints = [
            endpoint
            for port in RTSP_PORTS
            if (endpoint := _probe_rtsp(address, port)) is not None and endpoint["verified"]
        ]
        if endpoints:
            matched_rtsp.append({"ip": address, "endpoints": endpoints})
    devices = merge_discovery(matched_onvif, matched_rtsp, arp_entries)
    emit(f"RECOVERY: found {len(devices)} validated service candidate(s)")
    return devices


def scan_targeted_lan(
    targets: Iterable[dict[str, Any]],
    progress: Callable[[str, str, LanInterface], None] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    raw_log: list[str] = []

    def log(message: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        clean = " ".join(str(message).splitlines())
        if len(raw_log) < MAX_SCAN_LOG_LINES:
            raw_log.append(f"{timestamp} {clean}")

    log("RECOVERY: selecting the default private LAN interface")
    interface = default_lan_interface()
    targets = list(targets)
    if progress:
        progress("recovery", "running", interface)
    devices = discover_targeted(interface, targets, log)
    if progress:
        progress("recovery", "complete", interface)
    completed_at = datetime.now(timezone.utc).isoformat()
    duration_ms = round((time.monotonic() - started) * 1000)
    log(f"RECOVERY: complete in {duration_ms}ms")
    return {
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": duration_ms,
        "network": interface.as_dict(),
        "scanners": {"recovery": "complete"},
        "scanner_errors": {},
        "devices": devices,
        "raw_log": raw_log,
    }


def scan_lan(
    progress: Callable[[str, str, LanInterface], None] | None = None,
    known_devices: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    raw_log: list[str] = []
    log_lock = threading.Lock()

    def log(message: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        clean = " ".join(str(message).splitlines())
        with log_lock:
            if len(raw_log) < MAX_SCAN_LOG_LINES:
                raw_log.append(f"{timestamp} {clean}")

    log("SCAN: selecting the default private LAN interface")
    try:
        interface = default_lan_interface()
    except Exception as exc:
        log(f"SCAN: interface selection failed; {type(exc).__name__}: {str(exc)[:200]}")
        raise DiscoveryScanError(str(exc), raw_log) from exc
    log(
        f"SCAN: start; interface={interface.name}; address={interface.address}; "
        f"subnet={interface.network}; hosts={max(0, interface.network.num_addresses - 2)}"
    )
    known_devices = list(known_devices)
    scanners = {"onvif": "running", "rtsp": "running", "reachability": "running"}
    errors: dict[str, str] = {}
    if progress:
        for scanner in scanners:
            progress(scanner, "running", interface)
    results: dict[str, Any] = {"onvif": [], "rtsp": [], "reachability": []}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(discover_onvif, interface, log): "onvif",
            pool.submit(discover_rtsp, interface, log): "rtsp",
            pool.submit(discover_reachable_known, interface, known_devices, log): "reachability",
        }
        for future in concurrent.futures.as_completed(futures):
            scanner = futures[future]
            try:
                results[scanner] = future.result()
            except Exception as exc:
                scanners[scanner] = "error"
                errors[scanner] = str(exc)[:200]
                log(f"{scanner.upper()}: error; {type(exc).__name__}: {str(exc)[:200]}")
            else:
                scanners[scanner] = "complete"
            if progress:
                progress(scanner, scanners[scanner], interface)
    if "onvif" in errors and "rtsp" in errors:
        log("SCAN: failed; ONVIF and RTSP discovery both failed")
        raise DiscoveryScanError("ONVIF and RTSP discovery both failed", raw_log)
    onvif_devices = results["onvif"]
    rtsp_devices = results["rtsp"]
    arp_entries = read_arp_table()
    known_by_ip = {str(device.get("ip") or ""): device for device in known_devices}
    reachable_known: list[str] = []
    for address in results["reachability"]:
        expected_mac = str(known_by_ip.get(address, {}).get("mac") or "").lower()
        observed_mac = str(arp_entries.get(address) or "").lower()
        if expected_mac and observed_mac == expected_mac:
            reachable_known.append(address)
            log(f"REACHABILITY: {address} identity confirmed by MAC {observed_mac}")
        else:
            log(
                f"REACHABILITY: ignored {address}; MAC mismatch or unavailable "
                f"(expected={expected_mac or '-'}, observed={observed_mac or '-'})"
            )
    devices = merge_discovery(onvif_devices, rtsp_devices, arp_entries)
    duration_ms = round((time.monotonic() - started) * 1000)
    log(
        f"SCAN: complete in {duration_ms}ms; devices={len(devices)}; "
        f"onvif={len(onvif_devices)}; rtsp={len(rtsp_devices)}; "
        f"reachable_known={len(reachable_known)}; arp_entries={len(arp_entries)}"
    )
    return {
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "duration_ms": duration_ms,
        "network": interface.as_dict(),
        "scanners": scanners,
        "scanner_errors": errors,
        "devices": devices,
        "summary": {
            "devices": len(devices),
            "onvif": sum(1 for device in devices if device["onvif"]),
            "rtsp": sum(1 for device in devices if device["rtsp"]),
        },
        "raw_log": raw_log,
        "reachable_known": reachable_known,
    }
