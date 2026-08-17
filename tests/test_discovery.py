import ipaddress
import threading
import unittest
from unittest.mock import patch

from camadmiral import discovery


PROBE_RESPONSE = b"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
 <s:Body><d:ProbeMatches><d:ProbeMatch>
  <a:EndpointReference><a:Address>urn:uuid:camera-1</a:Address></a:EndpointReference>
  <d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>
  <d:Scopes>onvif://www.onvif.org/name/Front%20Door onvif://www.onvif.org/hardware/Model%2042 onvif://www.onvif.org/location/Office</d:Scopes>
  <d:XAddrs>http://192.168.10.20/onvif/device_service</d:XAddrs>
 </d:ProbeMatch></d:ProbeMatches></s:Body>
</s:Envelope>"""


class OnvifDiscoveryTests(unittest.TestCase):
    def test_probe_matches_are_parsed(self) -> None:
        matches = discovery.parse_probe_matches(PROBE_RESPONSE, "192.168.10.20")

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["name"], "Front Door")
        self.assertEqual(matches[0]["model"], "Model 42")
        self.assertNotIn("location", matches[0])
        self.assertFalse(any("/location/" in scope for scope in matches[0]["scopes"]))
        self.assertEqual(matches[0]["endpoint_reference"], "urn:uuid:camera-1")
        self.assertEqual(
            matches[0]["service_urls"],
            ["http://192.168.10.20/onvif/device_service"],
        )

    def test_malformed_probe_response_is_ignored(self) -> None:
        self.assertEqual(discovery.parse_probe_matches(b"not xml", "192.168.10.20"), [])

    def test_both_common_ws_discovery_dialects_are_sent(self) -> None:
        messages = discovery.onvif_probe_messages()

        self.assertEqual(len(messages), 4)
        self.assertIn(b"schemas.xmlsoap.org/ws/2005/04/discovery", messages[0])
        self.assertIn(b"NetworkVideoTransmitter", messages[1])
        self.assertIn(b"tds:Device", messages[2])
        self.assertIn(b"docs.oasis-open.org/ws-dd/ns/discovery/2009/01", messages[3])
        self.assertIn(b"<d:Probe/>", messages[0])

    def test_onvif_camera_signature_is_accepted(self) -> None:
        match = discovery.parse_probe_matches(PROBE_RESPONSE, "192.168.10.20")[0]
        self.assertTrue(discovery.is_onvif_camera(match))

    def test_generic_printer_discovery_response_is_rejected(self) -> None:
        printer = {
            "types": ["wprt:PrintDeviceType", "wsdp:Device"],
            "scopes": [],
            "service_urls": ["http://192.168.10.30:3911/"],
        }
        self.assertFalse(discovery.is_onvif_camera(printer))


class NetworkBoundaryTests(unittest.TestCase):
    ROUTES = """Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT
eth0 00000000 0128A8C0 0003 0 0 100 00000000 0 0 0
eth0 0028A8C0 00000000 0001 0 0 100 00FFFFFF 0 0 0
"""

    def test_default_private_lan_is_selected(self) -> None:
        def address(_name: str, request: int) -> ipaddress.IPv4Address:
            return ipaddress.IPv4Address("192.168.40.236" if request == 0x8915 else "255.255.255.0")

        with patch.object(discovery, "_interface_ipv4", side_effect=address):
            interface = discovery.default_lan_interface(self.ROUTES)

        self.assertEqual(interface.name, "eth0")
        self.assertEqual(str(interface.network), "192.168.40.0/24")

    def test_large_subnet_is_rejected(self) -> None:
        def address(_name: str, request: int) -> ipaddress.IPv4Address:
            return ipaddress.IPv4Address("10.1.2.3" if request == 0x8915 else "255.0.0.0")

        with patch.object(discovery, "_interface_ipv4", side_effect=address):
            with self.assertRaisesRegex(RuntimeError, "safety limit"):
                discovery.default_lan_interface(self.ROUTES)


class ResultTests(unittest.TestCase):
    def test_explicit_address_scan_stays_inside_selected_lan(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.10.2"),
            network=ipaddress.IPv4Network("192.168.10.0/24"),
        )
        with (
            patch.object(discovery, "default_lan_interface", return_value=interface),
            patch.object(discovery, "discover_onvif_address", return_value=[]),
            patch.object(discovery, "discover_rtsp_address", return_value=[]),
            patch.object(discovery, "read_arp_table", return_value={}),
        ):
            result = discovery.scan_explicit_address("192.168.10.20")
            with self.assertRaisesRegex(discovery.DiscoveryScanError, "inside"):
                discovery.scan_explicit_address("10.0.0.20")

        self.assertEqual(result["devices"], [])
        self.assertEqual(result["scanners"], {"onvif": "complete", "rtsp": "complete"})

    def test_onvif_and_rtsp_scanners_run_in_parallel(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.10.2"),
            network=ipaddress.IPv4Network("192.168.10.0/24"),
        )
        barrier = threading.Barrier(2)
        progress = []

        def onvif(_interface, _log=None):
            barrier.wait(timeout=1)
            return [{"ip": "192.168.10.20", "endpoint_reference": "uuid:test", "service_urls": [], "scopes": [], "types": [], "name": "Camera", "model": None}]

        def rtsp(_interface, _log=None):
            barrier.wait(timeout=1)
            return [{"ip": "192.168.10.20", "endpoints": []}]

        with (
            patch.object(discovery, "default_lan_interface", return_value=interface),
            patch.object(discovery, "discover_onvif", side_effect=onvif),
            patch.object(discovery, "discover_rtsp", side_effect=rtsp),
            patch.object(discovery, "read_arp_table", return_value={}),
        ):
            result = discovery.scan_lan(
                progress=lambda scanner, state, _interface: progress.append((scanner, state))
            )

        self.assertEqual(
            result["scanners"],
            {"onvif": "complete", "rtsp": "complete", "reachability": "complete"},
        )
        self.assertIn(("onvif", "running"), progress)
        self.assertIn(("rtsp", "running"), progress)
        self.assertIn(("onvif", "complete"), progress)
        self.assertIn(("rtsp", "complete"), progress)
        self.assertTrue(any("SCAN: start" in line for line in result["raw_log"]))
        self.assertTrue(any("SCAN: complete" in line for line in result["raw_log"]))

    def test_known_camera_reachability_is_bounded_to_private_lan_and_mac_devices(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.10.1"),
            network=ipaddress.IPv4Network("192.168.10.0/24"),
        )
        known = [
            {"ip": "192.168.10.20", "mac": "02:00:00:00:00:20"},
            {"ip": "192.168.10.21", "mac": None},
            {"ip": "10.0.0.20", "mac": "02:00:00:00:00:21"},
        ]

        with patch.object(discovery, "_ping_host", return_value=True) as ping:
            result = discovery.discover_reachable_known(interface, known)

        self.assertEqual(result, ["192.168.10.20"])
        ping.assert_called_once_with("192.168.10.20")

    def test_scan_requires_mac_match_before_confirming_known_reachability(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.10.1"),
            network=ipaddress.IPv4Network("192.168.10.0/24"),
        )
        known = [{"ip": "192.168.10.20", "mac": "02:00:00:00:00:20"}]
        with (
            patch.object(discovery, "default_lan_interface", return_value=interface),
            patch.object(discovery, "discover_onvif", return_value=[]),
            patch.object(discovery, "discover_rtsp", return_value=[]),
            patch.object(discovery, "discover_reachable_known", return_value=["192.168.10.20"]),
            patch.object(
                discovery,
                "read_arp_table",
                return_value={"192.168.10.20": "02:00:00:00:00:20"},
            ),
        ):
            matched = discovery.scan_lan(known_devices=known)

        with (
            patch.object(discovery, "default_lan_interface", return_value=interface),
            patch.object(discovery, "discover_onvif", return_value=[]),
            patch.object(discovery, "discover_rtsp", return_value=[]),
            patch.object(discovery, "discover_reachable_known", return_value=["192.168.10.20"]),
            patch.object(
                discovery,
                "read_arp_table",
                return_value={"192.168.10.20": "02:00:00:00:00:99"},
            ),
        ):
            mismatched = discovery.scan_lan(known_devices=known)

        self.assertEqual(matched["reachable_known"], ["192.168.10.20"])
        self.assertEqual(mismatched["reachable_known"], [])

    def test_rtsp_discovery_logs_but_rejects_non_rtsp_tcp_response(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.10.1"),
            network=ipaddress.IPv4Network("192.168.10.0/30"),
        )
        events = []

        def probe(address, port):
            if port == 554:
                return {
                    "port": port,
                    "url": f"rtsp://{address}:{port}",
                    "verified": False,
                    "response": None,
                    "latency_ms": 1,
                }
            return None

        with patch.object(discovery, "_probe_rtsp", side_effect=probe):
            result = discovery.discover_rtsp(interface, events.append)

        self.assertEqual(result, [])
        self.assertTrue(any("TCP open but response was not RTSP" in event for event in events))

    def test_onvif_and_rtsp_results_merge_by_ip_and_keep_mac(self) -> None:
        onvif = discovery.parse_probe_matches(PROBE_RESPONSE, "192.168.10.20")
        rtsp = [{"ip": "192.168.10.20", "endpoints": [{"port": 554, "url": "rtsp://192.168.10.20:554"}]}]

        devices = discovery.merge_discovery(
            onvif,
            rtsp,
            {"192.168.10.20": "aa:bb:cc:dd:ee:ff"},
        )

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["display_name"], "Front Door")
        self.assertEqual(devices[0]["mac"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(devices[0]["rtsp"][0]["port"], 554)

    def test_arp_parser_skips_incomplete_entries(self) -> None:
        table = """IP address HW type Flags HW address Mask Device
192.168.10.20 0x1 0x2 AA:BB:CC:DD:EE:FF * eth0
192.168.10.21 0x1 0x0 00:00:00:00:00:00 * eth0
"""

        self.assertEqual(
            discovery.read_arp_table(table),
            {"192.168.10.20": "aa:bb:cc:dd:ee:ff"},
        )

    def test_rtsp_probe_verifies_protocol_response(self) -> None:
        class FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def settimeout(self, _timeout: float) -> None:
                return None

            def sendall(self, payload: bytes) -> None:
                self.payload = payload

            def recv(self, _size: int) -> bytes:
                return b"RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n"

        with patch.object(discovery.socket, "create_connection", return_value=FakeSocket()):
            result = discovery._probe_rtsp("127.0.0.1", 8554)

        self.assertIsNotNone(result)
        self.assertTrue(result["verified"])
        self.assertEqual(result["response"], "RTSP/1.0 401 Unauthorized")

    def test_targeted_recovery_follows_one_unique_local_mac(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.10.1"),
            network=ipaddress.IPv4Network("192.168.10.0/24"),
        )
        endpoint = {
            "port": 554,
            "url": "rtsp://192.168.10.77:554",
            "verified": True,
            "response": "RTSP/1.0 401 Unauthorized",
            "latency_ms": 1,
        }
        with (
            patch.object(discovery, "discover_onvif", return_value=[]),
            patch.object(
                discovery,
                "read_arp_table",
                return_value={"192.168.10.77": "02:00:00:00:00:20"},
            ),
            patch.object(discovery, "_probe_rtsp", side_effect=lambda _address, port: endpoint if port == 554 else None),
        ):
            devices = discovery.discover_targeted(
                interface,
                [
                    {
                        "candidate_uuid": "candidate-1",
                        "mac": "02:00:00:00:00:20",
                        "endpoint_reference": None,
                    }
                ],
            )

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["ip"], "192.168.10.77")

    def test_targeted_recovery_rejects_duplicate_mac_observations(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.10.1"),
            network=ipaddress.IPv4Network("192.168.10.0/24"),
        )
        with (
            patch.object(discovery, "discover_onvif", return_value=[]),
            patch.object(
                discovery,
                "read_arp_table",
                return_value={
                    "192.168.10.77": "02:00:00:00:00:20",
                    "192.168.10.78": "02:00:00:00:00:20",
                },
            ),
            patch.object(discovery, "_probe_rtsp") as probe_rtsp,
        ):
            devices = discovery.discover_targeted(
                interface,
                [
                    {
                        "candidate_uuid": "candidate-1",
                        "mac": "02:00:00:00:00:20",
                        "endpoint_reference": None,
                    }
                ],
            )

        self.assertEqual(devices, [])
        probe_rtsp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
