"""On-demand, read-only discovery of local Frigate API endpoints."""
from __future__ import annotations

import asyncio
import json
import urllib.parse

from .frigate import REQUIRED_CAPABILITIES

LOCAL_PORTS = (5000, 8971, *range(20000, 21000))
WORKERS = 16
PROBE_TIMEOUT = 1.0
SCAN_TIMEOUT = 12.0
MAX_BODY = 1024 * 1024


async def _get(port: int, path: str) -> bytes:
    # No DNS, redirects, proxy environment, credentials, or caller-chosen host.
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n"
            "Accept: application/json\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        header = await reader.readuntil(b"\r\n\r\n")
        status = header.split(b"\r\n", 1)[0].split()
        if len(header) > 16384 or len(status) < 2 or not status[0].startswith(b"HTTP/") or status[1] != b"200":
            raise ValueError("Not an available API")
        body = bytearray()
        while chunk := await reader.read(min(65536, MAX_BODY + 1 - len(body))):
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise ValueError("Response too large")
        return bytes(body)
    finally:
        writer.close()


async def _probe(port: int) -> dict | None:
    try:
        raw = await asyncio.wait_for(_get(port, "/api/openapi.json"), PROBE_TIMEOUT)
        schema = json.loads(raw)
        paths = schema.get("paths") if isinstance(schema, dict) else None
        if not isinstance(paths, dict) or any(
            not isinstance(paths.get(path), dict) or method not in paths[path]
            for path, method in REQUIRED_CAPABILITIES.items()
        ):
            return None
        version = None
        try:
            raw_version = await asyncio.wait_for(_get(port, "/api/version"), PROBE_TIMEOUT)
            text = raw_version.decode().strip()
            if text.startswith('"'):
                text = json.loads(text)
            if isinstance(text, str) and 0 < len(text) <= 80 and all(
                char.isalnum() or char in ".-_+" for char in text
            ):
                version = text
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        return {"api_url": f"http://127.0.0.1:{port}", "version": version}
    except (OSError, ValueError, RecursionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return None


def _local_key(url: str) -> tuple | None:
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return None
        return parsed.scheme, parsed.port or (443 if parsed.scheme == "https" else 80), parsed.path.rstrip("/")
    except ValueError:
        return None


async def discover_local_frigates(existing: list[dict]) -> dict:
    ports = iter(LOCAL_PORTS)
    results = []
    known = {_local_key(target["api_url"]): target for target in existing}

    async def worker() -> None:
        for port in ports:
            found = await _probe(port)
            if found:
                target = known.get(_local_key(found["api_url"]))
                found["already_added"] = target is not None
                found["name"] = target["name"] if target else f"Frigate ({port})"
                results.append(found)

    timed_out = False
    try:
        await asyncio.wait_for(asyncio.gather(*(worker() for _ in range(WORKERS))), SCAN_TIMEOUT)
    except TimeoutError:
        timed_out = True
    results.sort(key=lambda item: urllib.parse.urlsplit(item["api_url"]).port)
    return {"instances": results, "timed_out": timed_out}
