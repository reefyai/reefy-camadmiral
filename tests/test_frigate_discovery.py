import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from camadmiral import app as app_module
from camadmiral import frigate_discovery as discovery
from camadmiral.frigate import REQUIRED_CAPABILITIES


class LocalDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()

    async def serve(self, *, schema=None, status=200, slow=False):
        async def handle(reader, writer):
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                if slow:
                    await reader.read()
                    return
                body = b"0.17.2" if b"/api/version " in request else json.dumps(schema).encode()
                writer.write(f"HTTP/1.0 {status} Test\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
                await writer.drain()
            finally:
                writer.close()
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def test_real_http_identification_version_and_localhost_duplicate(self):
        schema = {"paths": {path: {method: {}} for path, method in REQUIRED_CAPABILITIES.items()}}
        first = await self.serve(schema=schema)
        second = await self.serve(schema=schema)
        other = await self.serve(schema={"version": "0.17.2", "paths": {}})
        denied = await self.serve(schema=schema, status=401)
        redirect = await self.serve(schema=schema, status=302)
        with patch.object(discovery, "LOCAL_PORTS", (first, second, other, denied, redirect)):
            result = await discovery.discover_local_frigates([
                {"api_url": f"http://localhost:{first}/", "name": "Existing"}
            ])
        self.assertFalse(result["timed_out"])
        self.assertEqual(len(result["instances"]), 2)
        by_url = {item["api_url"]: item for item in result["instances"]}
        self.assertTrue(by_url[f"http://127.0.0.1:{first}"]["already_added"])
        self.assertFalse(by_url[f"http://127.0.0.1:{second}"]["already_added"])
        self.assertEqual(by_url[f"http://127.0.0.1:{second}"]["version"], "0.17.2")

    async def test_slow_service_is_bounded(self):
        port = await self.serve(slow=True)
        with patch.object(discovery, "PROBE_TIMEOUT", 0.03):
            self.assertIsNone(await asyncio.wait_for(discovery._probe(port), 0.5))

    async def test_invalid_response_is_ignored_and_missing_version_is_allowed(self):
        with patch.object(discovery, "_get", AsyncMock(return_value=b"not json")):
            self.assertIsNone(await discovery._probe(5000))
        schema = json.dumps({"paths": {path: {method: {}} for path, method in REQUIRED_CAPABILITIES.items()}}).encode()
        with patch.object(discovery, "_get", AsyncMock(side_effect=[schema, OSError("offline")])):
            found = await discovery._probe(5000)
        self.assertEqual(found, {"api_url": "http://127.0.0.1:5000", "version": None})

    async def test_scan_deadline_cancels_workers_and_returns_partial_results(self):
        active = 0
        peak = 0
        async def probe(port):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                if port == 5000:
                    return {"api_url": "http://127.0.0.1:5000", "version": "0.17.2"}
                await asyncio.sleep(10)
            finally:
                active -= 1
        with patch.object(discovery, "SCAN_TIMEOUT", 0.03), patch.object(discovery, "_probe", probe):
            result = await discovery.discover_local_frigates([])
        self.assertTrue(result["timed_out"])
        self.assertEqual(len(result["instances"]), 1)
        self.assertLessEqual(peak, discovery.WORKERS)
        self.assertEqual(active, 0)

    async def test_endpoint_requires_action_and_does_not_mutate_integrations(self):
        with self.assertRaises(HTTPException):
            await app_module.find_local_frigates(None)
        repository = Mock()
        repository.frigate_targets.return_value = []
        with patch.object(app_module, "_repository", return_value=repository), patch.object(
            app_module, "discover_local_frigates", AsyncMock(return_value={"instances": [], "timed_out": False})
        ) as find:
            response = await app_module.find_local_frigates("find-frigate")
            self.assertEqual(response.status_code, 200)
            self.assertEqual([call[0] for call in repository.mock_calls], ["frigate_targets"])
            app_module._frigate_discovery_lock.acquire()
            try:
                response = await app_module.find_local_frigates("find-frigate")
                self.assertEqual(response.status_code, 409)
                self.assertEqual(find.await_count, 1)
            finally:
                app_module._frigate_discovery_lock.release()

    async def test_endpoint_releases_lock_after_failure(self):
        repository = Mock()
        repository.frigate_targets.return_value = []
        with patch.object(app_module, "_repository", return_value=repository), patch.object(
            app_module, "discover_local_frigates", AsyncMock(side_effect=RuntimeError("test"))
        ):
            with self.assertRaises(RuntimeError):
                await app_module.find_local_frigates("find-frigate")
        self.assertFalse(app_module._frigate_discovery_lock.locked())

    def test_fixed_port_scope_and_duplicate_paths(self):
        self.assertEqual(set(discovery.LOCAL_PORTS), {5000, 8971, *range(20000, 21000)})
        self.assertIsNone(discovery._local_key("http://remote.invalid:5000"))
        self.assertNotEqual(discovery._local_key("http://localhost:5000/prefix"), discovery._local_key("http://127.0.0.1:5000"))
