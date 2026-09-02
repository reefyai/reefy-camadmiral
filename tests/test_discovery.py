import ipaddress
import socket
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
        candidate = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.40.236"),
            network=ipaddress.IPv4Network("192.168.40.0/24"),
        )

        def address(_name: str, request: int) -> ipaddress.IPv4Address:
            return ipaddress.IPv4Address("192.168.40.236" if request == 0x8915 else "255.255.255.0")

        with patch.object(discovery, "_interface_ipv4", side_effect=address):
            interfaces = discovery.private_lan_interfaces(self.ROUTES, [candidate])

        self.assertEqual(interfaces, [candidate])

    def test_all_safe_private_subnets_are_selected_with_default_first(self) -> None:
        default = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.40.236"),
            network=ipaddress.IPv4Network("192.168.40.0/24"),
        )
        secondary = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.10.1"),
            network=ipaddress.IPv4Network("192.168.10.0/24"),
        )
        other_lan = discovery.LanInterface(
            name="eth1",
            address=ipaddress.IPv4Address("10.20.30.1"),
            network=ipaddress.IPv4Network("10.20.30.0/24"),
        )
        docker_bridge = discovery.LanInterface(
            name="docker0",
            address=ipaddress.IPv4Address("172.17.0.1"),
            network=ipaddress.IPv4Network("172.17.0.0/16"),
        )

        with patch.object(discovery, "_interface_ipv4", return_value=default.address):
            interfaces = discovery.private_lan_interfaces(
                self.ROUTES,
                [secondary, docker_bridge, other_lan, default],
            )

        self.assertEqual(interfaces, [default, secondary, other_lan])

    def test_large_subnet_is_selected_for_multicast_only_discovery(self) -> None:
        candidate = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("10.1.2.3"),
            network=ipaddress.IPv4Network("10.0.0.0/16"),
        )

        with patch.object(discovery, "_interface_ipv4", return_value=candidate.address):
            interfaces = discovery.private_lan_interfaces(self.ROUTES, [candidate])

        self.assertEqual(interfaces, [candidate])
        self.assertFalse(discovery.sweep_allowed(candidate.network))
        self.assertTrue(discovery.sweep_allowed(ipaddress.IPv4Network("10.0.0.0/24")))

    def test_rtsp_sweep_is_skipped_on_large_subnet(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("10.1.2.3"),
            network=ipaddress.IPv4Network("10.0.0.0/16"),
        )
        lines: list[str] = []

        with patch.object(discovery, "_probe_rtsp") as probe:
            result = discovery.discover_rtsp(interface, lines.append)

        self.assertEqual(result, [])
        probe.assert_not_called()
        self.assertTrue(any("sweep limit" in line for line in lines))

    def test_learned_neighbors_are_filtered_to_the_interface_and_bounded(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("10.0.2.3"),
            network=ipaddress.IPv4Network("10.0.0.0/16"),
        )
        entries = {
            "not-an-address": "02:00:00:00:00:01",
            "10.0.2.3": "02:00:00:00:00:02",
            "10.0.2.20": "02:00:00:00:00:20",
            "10.0.2.10": "02:00:00:00:00:10",
            "192.168.1.20": "02:00:00:00:00:30",
        }

        with patch.object(discovery, "MAX_SCAN_HOSTS", 1):
            addresses = discovery.learned_neighbor_addresses(interface, entries)

        self.assertEqual(addresses, ["10.0.2.10"])

    def test_custom_subnets_are_normalized_and_bounded(self) -> None:
        self.assertEqual(
            str(discovery.custom_scan_subnet("10.0.202.41/24")),
            "10.0.202.0/24",
        )
        self.assertEqual(
            str(discovery.custom_scan_subnet("172.20.0.0/22")),
            "172.20.0.0/22",
        )
        with self.assertRaisesRegex(ValueError, "private IPv4"):
            discovery.custom_scan_subnet("192.0.2.0/24")
        with self.assertRaisesRegex(ValueError, "1024"):
            discovery.custom_scan_subnet("10.0.0.0/8")

    def test_routed_subnet_uses_the_longest_prefix_route_and_source_address(self) -> None:
        routes = """Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT
eth0 00000000 0128A8C0 0003 0 0 100 00000000 0 0 0
eth1 00CA000A 010110AC 0003 0 0 20 00FFFFFF 0 0 0
"""
        eth0 = discovery.LanInterface(
            "eth0",
            ipaddress.IPv4Address("192.168.40.236"),
            ipaddress.IPv4Network("192.168.40.0/24"),
        )
        eth1 = discovery.LanInterface(
            "eth1",
            ipaddress.IPv4Address("172.16.1.2"),
            ipaddress.IPv4Network("172.16.1.0/24"),
        )
        with patch.object(
            discovery,
            "_interface_ipv4",
            return_value=eth1.address,
        ):
            routed = discovery.routed_scan_interface(
                ipaddress.IPv4Network("10.0.202.0/24"),
                route_text=routes,
                interfaces=[eth0, eth1],
            )

        self.assertEqual(routed.name, "eth1")
        self.assertEqual(str(routed.address), "172.16.1.2")
        self.assertEqual(str(routed.network), "10.0.202.0/24")
        self.assertFalse(routed.directly_connected)


class _FakeUdpSocket:
    """Capture sendto destinations; recvfrom immediately times out."""

    def __init__(self, *_args, **_kwargs):
        self.destinations: list[tuple[str, int]] = []
        _FakeUdpSocket.last = self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def setsockopt(self, *_args):
        pass

    def bind(self, _address):
        pass

    def settimeout(self, _value):
        pass

    def sendto(self, _payload, destination):
        self.destinations.append(destination)

    def recvfrom(self, _size):
        raise socket.timeout


class OnvifSweepFallbackTests(unittest.TestCase):
    def _run(self, network: str) -> list[tuple[str, int]]:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("10.0.0.2"),
            network=ipaddress.IPv4Network(network),
        )
        with patch.object(discovery.socket, "socket", _FakeUdpSocket):
            discovery.discover_onvif(interface)
        return _FakeUdpSocket.last.destinations

    def test_small_subnet_sends_multicast_first_then_unicast_fallback(self) -> None:
        destinations = self._run("10.0.0.0/29")

        multicast = [d for d in destinations if d == discovery.ONVIF_MULTICAST]
        unicast = [d for d in destinations if d != discovery.ONVIF_MULTICAST]
        self.assertEqual(len(multicast), 4)
        self.assertEqual(destinations[:4], multicast)
        self.assertEqual(
            sorted(d[0] for d in unicast),
            ["10.0.0.1", "10.0.0.3", "10.0.0.4", "10.0.0.5", "10.0.0.6"],
        )

    def test_large_subnet_uses_multicast_only(self) -> None:
        destinations = self._run("10.0.0.0/16")

        self.assertEqual(destinations, [discovery.ONVIF_MULTICAST] * 4)

    def test_large_subnet_uses_learned_neighbors_for_unicast_fallback(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("10.0.0.2"),
            network=ipaddress.IPv4Network("10.0.0.0/16"),
        )
        with patch.object(discovery.socket, "socket", _FakeUdpSocket):
            discovery.discover_onvif(
                interface,
                fallback_addresses=["10.0.3.20", "192.168.1.20"],
            )

        self.assertEqual(
            _FakeUdpSocket.last.destinations,
            [discovery.ONVIF_MULTICAST] * 4
            + [("10.0.3.20", discovery.ONVIF_MULTICAST[1])],
        )

    def test_routed_subnet_skips_multicast_and_uses_unicast_only(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.40.2"),
            network=ipaddress.IPv4Network("10.0.202.0/30"),
            directly_connected=False,
        )
        with patch.object(discovery.socket, "socket", _FakeUdpSocket):
            discovery.discover_onvif(interface)

        self.assertEqual(
            _FakeUdpSocket.last.destinations,
            [
                ("10.0.202.1", discovery.ONVIF_MULTICAST[1]),
                ("10.0.202.2", discovery.ONVIF_MULTICAST[1]),
            ],
        )


class ResultTests(unittest.TestCase):
    def test_explicit_address_scan_accepts_any_connected_lan(self) -> None:
        primary = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.10.2"),
            network=ipaddress.IPv4Network("192.168.10.0/24"),
        )
        secondary = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.40.2"),
            network=ipaddress.IPv4Network("192.168.40.0/24"),
        )
        with (
            patch.object(
                discovery,
                "private_lan_interfaces",
                return_value=[primary, secondary],
            ),
            patch.object(discovery, "discover_onvif_address", return_value=[]),
            patch.object(discovery, "discover_rtsp_address", return_value=[]),
            patch.object(discovery, "read_arp_table", return_value={}),
        ):
            primary_result = discovery.scan_explicit_address("192.168.10.20")
            secondary_result = discovery.scan_explicit_address("192.168.40.87")
            with self.assertRaisesRegex(discovery.DiscoveryScanError, "connected LAN"):
                discovery.scan_explicit_address("10.0.0.20")

        self.assertEqual(primary_result["network"]["subnet"], "192.168.10.0/24")
        self.assertEqual(secondary_result["network"]["subnet"], "192.168.40.0/24")
        self.assertEqual(
            secondary_result["scanners"], {"onvif": "complete", "rtsp": "complete"}
        )

    def test_full_scan_combines_devices_from_every_connected_lan(self) -> None:
        primary = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.40.2"),
            network=ipaddress.IPv4Network("192.168.40.0/24"),
        )
        secondary = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.10.1"),
            network=ipaddress.IPv4Network("192.168.10.0/24"),
        )

        def rtsp(interface, _log=None, _executor=None):
            camera_address = (
                "192.168.40.20"
                if interface == primary
                else "192.168.10.87"
            )
            return [{"ip": camera_address, "endpoints": [{"port": 554}]}]

        with (
            patch.object(
                discovery,
                "private_lan_interfaces",
                return_value=[primary, secondary],
            ),
            patch.object(discovery, "discover_onvif", return_value=[]),
            patch.object(discovery, "discover_rtsp", side_effect=rtsp),
            patch.object(discovery, "discover_reachable_known", return_value=[]),
            patch.object(discovery, "read_arp_table", return_value={}),
        ):
            result = discovery.scan_lan()

        self.assertEqual(
            [device["ip"] for device in result["devices"]],
            ["192.168.10.87", "192.168.40.20"],
        )
        self.assertEqual(result["network"]["subnet"], "192.168.40.0/24")
        self.assertTrue(
            any("subnet=192.168.10.0/24" in line for line in result["raw_log"])
        )
        self.assertEqual(
            [network["status"] for network in result["networks"]],
            ["complete", "complete"],
        )
        self.assertTrue(all(network["multicast"] for network in result["networks"]))

    def test_full_scan_reports_custom_routed_subnet_progress(self) -> None:
        routed = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.40.2"),
            network=ipaddress.IPv4Network("10.0.202.0/30"),
            directly_connected=False,
        )
        progress: list[tuple[str, str, str]] = []
        with (
            patch.object(
                discovery,
                "selected_scan_interfaces",
                return_value=([routed], {}),
            ),
            patch.object(discovery, "discover_onvif", return_value=[]) as onvif,
            patch.object(discovery, "discover_rtsp", return_value=[]),
            patch.object(discovery, "discover_reachable_known", return_value=[]),
            patch.object(discovery, "read_arp_table", return_value={}),
        ):
            result = discovery.scan_lan(
                subnets=["10.0.202.0/30"],
                progress=lambda scanner, state, interface: progress.append(
                    (scanner, state, str(interface.network))
                ),
            )

        self.assertFalse(onvif.call_args.args[0].directly_connected)
        self.assertEqual(result["networks"][0]["subnet"], "10.0.202.0/30")
        self.assertEqual(result["networks"][0]["status"], "complete")
        self.assertFalse(result["networks"][0]["multicast"])
        self.assertIn(("onvif", "running", "10.0.202.0/30"), progress)
        self.assertIn(("onvif", "complete", "10.0.202.0/30"), progress)

    def test_full_scan_on_large_subnet_skips_rtsp_and_keeps_multicast_results(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("10.0.0.2"),
            network=ipaddress.IPv4Network("10.0.0.0/16"),
        )
        camera = {
            "ip": "10.0.3.20",
            "endpoint_reference": "urn:uuid:camera-1",
            "service_urls": ["http://10.0.3.20/onvif/device_service"],
            "scopes": [],
            "types": [],
            "name": "Warehouse",
            "model": None,
        }
        progress = []

        with (
            patch.object(discovery, "private_lan_interfaces", return_value=[interface]),
            patch.object(discovery, "discover_onvif", return_value=[camera]),
            patch.object(discovery, "discover_rtsp") as rtsp,
            patch.object(discovery, "discover_reachable_known", return_value=[]),
            patch.object(discovery, "read_arp_table", return_value={}),
        ):
            result = discovery.scan_lan(
                progress=lambda scanner, state, _interface: progress.append((scanner, state))
            )

        rtsp.assert_not_called()
        self.assertEqual(
            result["scanners"],
            {"onvif": "complete", "rtsp": "skipped", "reachability": "complete"},
        )
        self.assertEqual([device["ip"] for device in result["devices"]], ["10.0.3.20"])
        self.assertIn(("rtsp", "skipped"), progress)
        self.assertTrue(any("sweep limit" in line for line in result["raw_log"]))

    def test_full_scan_on_large_subnet_probes_learned_neighbors(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("10.0.0.2"),
            network=ipaddress.IPv4Network("10.0.0.0/16"),
        )
        arp_entries = {
            "10.0.3.20": "02:00:00:00:00:20",
            "192.168.1.20": "02:00:00:00:00:21",
        }
        rtsp_camera = {
            "ip": "10.0.3.20",
            "endpoints": [{"port": 554, "verified": True}],
        }

        with (
            patch.object(discovery, "private_lan_interfaces", return_value=[interface]),
            patch.object(discovery, "discover_onvif", return_value=[]) as onvif,
            patch.object(discovery, "discover_rtsp") as full_rtsp,
            patch.object(
                discovery,
                "discover_rtsp_neighbors",
                return_value=[rtsp_camera],
            ) as neighbor_rtsp,
            patch.object(discovery, "discover_reachable_known", return_value=[]),
            patch.object(discovery, "read_arp_table", return_value=arp_entries),
        ):
            result = discovery.scan_lan()

        full_rtsp.assert_not_called()
        onvif.assert_called_once_with(interface, unittest.mock.ANY, ["10.0.3.20"])
        neighbor_rtsp.assert_called_once_with(
            interface,
            ["10.0.3.20"],
            unittest.mock.ANY,
            unittest.mock.ANY,
        )
        self.assertEqual(result["scanners"]["rtsp"], "complete")
        self.assertEqual([device["ip"] for device in result["devices"]], ["10.0.3.20"])

    def test_full_scan_fails_when_onvif_errors_and_rtsp_is_skipped(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("10.0.0.2"),
            network=ipaddress.IPv4Network("10.0.0.0/16"),
        )

        with (
            patch.object(discovery, "private_lan_interfaces", return_value=[interface]),
            patch.object(discovery, "discover_onvif", side_effect=OSError("multicast failed")),
            patch.object(discovery, "discover_rtsp") as rtsp,
            patch.object(discovery, "discover_reachable_known", return_value=[]),
            patch.object(discovery, "read_arp_table", return_value={}),
        ):
            with self.assertRaisesRegex(discovery.DiscoveryScanError, "no RTSP sweep"):
                discovery.scan_lan()

        rtsp.assert_not_called()

    def test_onvif_and_rtsp_scanners_run_in_parallel(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("192.168.10.2"),
            network=ipaddress.IPv4Network("192.168.10.0/24"),
        )
        barrier = threading.Barrier(2)
        progress = []

        def onvif(_interface, _log=None, _fallback_addresses=()):
            barrier.wait(timeout=1)
            return [{"ip": "192.168.10.20", "endpoint_reference": "uuid:test", "service_urls": [], "scopes": [], "types": [], "name": "Camera", "model": None}]

        def rtsp(_interface, _log=None, _executor=None):
            barrier.wait(timeout=1)
            return [{"ip": "192.168.10.20", "endpoints": []}]

        with (
            patch.object(discovery, "private_lan_interfaces", return_value=[interface]),
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

    def test_full_scan_shares_bounded_worker_pools_across_subnets(self) -> None:
        interfaces = [
            discovery.LanInterface(
                name="eth0",
                address=ipaddress.IPv4Address("192.168.10.2"),
                network=ipaddress.IPv4Network("192.168.10.0/24"),
            ),
            discovery.LanInterface(
                name="eth1",
                address=ipaddress.IPv4Address("192.168.20.2"),
                network=ipaddress.IPv4Network("192.168.20.0/24"),
            ),
        ]
        rtsp_executors = []
        reachability_executors = []

        def rtsp(_interface, _log=None, executor=None):
            rtsp_executors.append(executor)
            return []

        def reachability(_interface, _known, _log=None, executor=None):
            reachability_executors.append(executor)
            return []

        with (
            patch.object(discovery, "private_lan_interfaces", return_value=interfaces),
            patch.object(discovery, "discover_onvif", return_value=[]),
            patch.object(discovery, "discover_rtsp", side_effect=rtsp),
            patch.object(
                discovery,
                "discover_reachable_known",
                side_effect=reachability,
            ),
            patch.object(discovery, "read_arp_table", return_value={}),
        ):
            result = discovery.scan_lan()

        self.assertEqual(result["scanners"]["rtsp"], "complete")
        self.assertEqual(len(rtsp_executors), 2)
        self.assertIs(rtsp_executors[0], rtsp_executors[1])
        self.assertEqual(len(reachability_executors), 2)
        self.assertIs(reachability_executors[0], reachability_executors[1])
        self.assertIsNot(rtsp_executors[0], reachability_executors[0])

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
            patch.object(discovery, "private_lan_interfaces", return_value=[interface]),
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
            patch.object(discovery, "private_lan_interfaces", return_value=[interface]),
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
        logs: list[str] = []
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
                logs.append,
            )

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["ip"], "192.168.10.77")
        self.assertIn("RECOVERY: target candidate candidate-1", logs)

    def test_targeted_recovery_follows_onvif_identity_when_mac_changes(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("172.21.10.1"),
            network=ipaddress.IPv4Network("172.21.10.0/24"),
        )
        onvif = {
            "ip": "172.21.10.77",
            "endpoint_reference": "urn:uuid:synthetic-camera",
            "service_urls": ["http://172.21.10.77/onvif/device_service"],
            "scopes": [],
            "types": [],
            "name": "Synthetic camera",
            "model": None,
        }
        endpoint = {
            "port": 554,
            "url": "rtsp://172.21.10.77:554",
            "verified": True,
            "response": "RTSP/1.0 401 Unauthorized",
            "latency_ms": 1,
        }
        with (
            patch.object(discovery, "discover_onvif", return_value=[onvif]),
            patch.object(
                discovery,
                "read_arp_table",
                return_value={"172.21.10.77": "02:00:00:00:00:77"},
            ),
            patch.object(
                discovery,
                "_probe_rtsp",
                side_effect=lambda _address, port: endpoint if port == 554 else None,
            ),
        ):
            devices = discovery.discover_targeted(
                interface,
                [
                    {
                        "candidate_uuid": "candidate-1",
                        "mac": "02:00:00:00:00:20",
                        "endpoint_reference": "urn:uuid:synthetic-camera",
                    }
                ],
            )

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["ip"], "172.21.10.77")
        self.assertEqual(devices[0]["mac"], "02:00:00:00:00:77")
        self.assertEqual(
            devices[0]["onvif"]["endpoint_reference"],
            "urn:uuid:synthetic-camera",
        )

    def test_targeted_recovery_reserves_onvif_match_before_recycled_mac(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("172.21.10.1"),
            network=ipaddress.IPv4Network("172.21.10.0/24"),
        )
        onvif = {
            "ip": "172.21.10.77",
            "endpoint_reference": "urn:uuid:camera-a",
            "service_urls": ["http://172.21.10.77/onvif/device_service"],
            "scopes": [],
            "types": [],
            "name": "Synthetic camera A",
            "model": None,
        }
        endpoint = {
            "port": 554,
            "url": "rtsp://172.21.10.77:554",
            "verified": True,
            "response": "RTSP/1.0 401 Unauthorized",
            "latency_ms": 1,
        }
        with (
            patch.object(discovery, "discover_onvif", return_value=[onvif]),
            patch.object(
                discovery,
                "read_arp_table",
                return_value={"172.21.10.77": "02:00:00:00:00:22"},
            ),
            patch.object(
                discovery,
                "_probe_rtsp",
                side_effect=lambda _address, port: endpoint if port == 554 else None,
            ),
        ):
            devices = discovery.discover_targeted(
                interface,
                [
                    {
                        "candidate_uuid": "candidate-a",
                        "mac": "02:00:00:00:00:11",
                        "endpoint_reference": "urn:uuid:camera-a",
                    },
                    {
                        "candidate_uuid": "candidate-b",
                        "mac": "02:00:00:00:00:22",
                        "endpoint_reference": "urn:uuid:camera-b",
                    },
                ],
            )

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["ip"], "172.21.10.77")
        self.assertEqual(
            devices[0]["onvif"]["endpoint_reference"],
            "urn:uuid:camera-a",
        )

    def test_targeted_recovery_rejects_duplicate_mac_observations(self) -> None:
        interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("172.21.10.1"),
            network=ipaddress.IPv4Network("172.21.10.0/24"),
        )
        with (
            patch.object(discovery, "discover_onvif", return_value=[]),
            patch.object(
                discovery,
                "read_arp_table",
                return_value={
                    "172.21.10.77": "02:00:00:00:00:20",
                    "172.21.10.78": "02:00:00:00:00:20",
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

    def test_targeted_recovery_uses_selected_routed_subnet(self) -> None:
        routed = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("172.20.0.2"),
            network=ipaddress.IPv4Network("172.20.37.0/30"),
            directly_connected=False,
        )
        target = {
            "candidate_uuid": "candidate-1",
            "mac": None,
            "endpoint_reference": "urn:uuid:synthetic-camera",
        }
        with (
            patch.object(
                discovery,
                "selected_scan_interfaces",
                return_value=([routed], {}),
            ) as selected,
            patch.object(discovery, "private_lan_interfaces") as connected,
            patch.object(discovery, "discover_targeted", return_value=[]) as recover,
        ):
            result = discovery.scan_targeted_lan(
                [target],
                subnets=["172.20.37.0/30"],
            )

        selected.assert_called_once_with(["172.20.37.0/30"])
        connected.assert_not_called()
        recover.assert_called_once_with(routed, [target], unittest.mock.ANY)
        self.assertEqual(result["network"]["subnet"], "172.20.37.0/30")

    def test_targeted_recovery_deduplicates_overlapping_subnet_observations(self) -> None:
        connected = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("172.20.0.2"),
            network=ipaddress.IPv4Network("172.20.0.0/16"),
        )
        routed = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("172.20.0.2"),
            network=ipaddress.IPv4Network("172.20.37.0/24"),
            directly_connected=False,
        )
        observed = {
            "ip": "172.20.37.20",
            "mac": "02:00:00:00:00:20",
            "display_name": "Synthetic camera",
            "onvif": {
                "endpoint_reference": "urn:uuid:synthetic-camera",
                "service_urls": ["http://172.20.37.20/onvif/device_service"],
            },
            "rtsp": [],
        }
        with (
            patch.object(
                discovery,
                "selected_scan_interfaces",
                return_value=([connected, routed], {}),
            ),
            patch.object(discovery, "discover_targeted", return_value=[observed]),
        ):
            result = discovery.scan_targeted_lan(
                [{"candidate_uuid": "candidate-1"}],
                subnets=["172.20.0.0/16", "172.20.37.0/24"],
            )

        self.assertEqual(result["devices"], [observed])

    def test_targeted_recovery_deduplicates_partial_overlap_observations(self) -> None:
        connected = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("172.20.0.2"),
            network=ipaddress.IPv4Network("172.20.0.0/16"),
        )
        routed = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("172.20.0.2"),
            network=ipaddress.IPv4Network("172.20.37.0/24"),
            directly_connected=False,
        )

        def observation(interface, _targets, _log):
            return [{
                "ip": "172.20.37.20",
                "mac": (
                    "02:00:00:00:00:20"
                    if interface is connected
                    else None
                ),
                "display_name": "Synthetic camera",
                "onvif": {
                    "endpoint_reference": "urn:uuid:synthetic-camera",
                    "service_urls": [
                        "http://172.20.37.20/onvif/device_service"
                    ],
                },
                "rtsp": [],
            }]

        with (
            patch.object(
                discovery,
                "selected_scan_interfaces",
                return_value=([connected, routed], {}),
            ),
            patch.object(discovery, "discover_targeted", side_effect=observation),
        ):
            result = discovery.scan_targeted_lan(
                [{"candidate_uuid": "candidate-1"}],
                subnets=["172.20.0.0/16", "172.20.37.0/24"],
            )

        self.assertEqual(len(result["devices"]), 1)
        self.assertEqual(result["devices"][0]["mac"], "02:00:00:00:00:20")

    def test_targeted_recovery_keeps_conflicting_stable_observations(self) -> None:
        first = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("172.21.10.2"),
            network=ipaddress.IPv4Network("172.21.10.0/24"),
        )
        second = discovery.LanInterface(
            name="eth1",
            address=ipaddress.IPv4Address("172.21.20.2"),
            network=ipaddress.IPv4Network("172.21.20.0/24"),
        )

        def observation(interface, _targets, _log):
            suffix = "10" if interface is first else "20"
            return [{
                "ip": "172.21.10.20",
                "mac": f"02:00:00:00:00:{suffix}",
                "display_name": "Synthetic camera",
                "onvif": {"endpoint_reference": f"urn:uuid:camera-{suffix}"},
                "rtsp": [],
            }]

        with (
            patch.object(
                discovery,
                "selected_scan_interfaces",
                return_value=([first, second], {}),
            ),
            patch.object(discovery, "discover_targeted", side_effect=observation),
        ):
            result = discovery.scan_targeted_lan(
                [{"candidate_uuid": "candidate-1"}],
                subnets=["172.21.10.0/24", "172.21.20.0/24"],
            )

        self.assertEqual(len(result["devices"]), 2)

    def test_targeted_recovery_keeps_same_identity_at_different_addresses(self) -> None:
        observations = [
            {
                "ip": address,
                "mac": "02:00:00:00:00:20",
                "onvif": {"endpoint_reference": "urn:uuid:synthetic-camera"},
                "rtsp": [],
            }
            for address in ("172.21.10.20", "172.21.10.21")
        ]

        result = discovery._deduplicate_targeted_devices(observations)

        self.assertEqual(result, observations)

    def test_targeted_recovery_without_subnets_uses_connected_interfaces(self) -> None:
        connected_interface = discovery.LanInterface(
            name="eth0",
            address=ipaddress.IPv4Address("172.21.10.2"),
            network=ipaddress.IPv4Network("172.21.10.0/24"),
        )
        with (
            patch.object(
                discovery,
                "private_lan_interfaces",
                return_value=[connected_interface],
            ) as connected,
            patch.object(discovery, "selected_scan_interfaces") as selected,
            patch.object(discovery, "discover_targeted", return_value=[]),
        ):
            discovery.scan_targeted_lan([])

        connected.assert_called_once_with()
        selected.assert_not_called()


if __name__ == "__main__":
    unittest.main()
