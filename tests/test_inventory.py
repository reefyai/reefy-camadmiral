import json
import ipaddress
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camadmiral import inventory, scanner_worker


def camera(address: str, mac: str, endpoint: str = "uuid:camera-1") -> dict:
    return {
        "ip": address,
        "mac": mac,
        "display_name": "Test camera",
        "onvif": {"endpoint_reference": endpoint, "service_urls": []},
        "rtsp": [],
    }


class InventoryTests(unittest.TestCase):
    def test_matching_reachable_camera_stays_online_when_services_are_silent(self) -> None:
        previous = inventory.reconcile_inventory(
            [],
            [camera("192.168.1.20", "aa:bb:cc:dd:ee:ff")],
            "t1",
        )

        devices = inventory.reconcile_inventory(
            previous,
            [],
            "t2",
            reachable_ips=["192.168.1.20"],
        )

        self.assertEqual(devices[0]["status"], "online")
        self.assertEqual(devices[0]["service_status"], "unavailable")
        self.assertEqual(devices[0]["last_seen"], "t2")
        self.assertEqual(devices[0]["last_service_seen"], "t1")
        self.assertEqual(devices[0]["missed_scans"], 0)

    def test_partial_scan_preserves_known_protocol_metadata_at_same_ip(self) -> None:
        previous = [
            {
                "candidate_uuid": "candidate-1",
                "ip": "192.168.1.20",
                "mac": "02:00:00:00:00:20",
                "display_name": "Synthetic camera",
                "onvif": {"service_urls": ["http://192.168.1.20/onvif/device_service"]},
                "rtsp": [{"url": "rtsp://192.168.1.20:554"}],
                "status": "online",
            }
        ]
        current = [
            {
                "ip": "192.168.1.20",
                "mac": "02:00:00:00:00:20",
                "display_name": "192.168.1.20",
                "onvif": None,
                "rtsp": [{"url": "rtsp://192.168.1.20:554"}],
            }
        ]

        result = inventory.reconcile_inventory(previous, current, "2026-01-01T00:00:00+00:00")

        self.assertEqual(result[0]["display_name"], "Synthetic camera")
        self.assertEqual(result[0]["onvif"], previous[0]["onvif"])

    def test_new_camera_is_online(self) -> None:
        devices = inventory.reconcile_inventory([], [camera("192.168.1.20", "aa:bb:cc:dd:ee:ff")], "t1")

        self.assertEqual(devices[0]["status"], "online")
        self.assertEqual(devices[0]["first_seen"], "t1")
        self.assertEqual(devices[0]["last_seen"], "t1")
        self.assertEqual(devices[0]["missed_scans"], 0)
        self.assertTrue(devices[0]["candidate_uuid"])

    def test_missing_camera_is_retained_offline(self) -> None:
        previous = inventory.reconcile_inventory([], [camera("192.168.1.20", "aa:bb:cc:dd:ee:ff")], "t1")

        devices = inventory.reconcile_inventory(previous, [], "t2")

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["status"], "offline")
        self.assertEqual(devices[0]["last_seen"], "t1")
        self.assertEqual(devices[0]["missed_scans"], 1)

    def test_selected_subnet_scan_preserves_devices_outside_its_evidence(self) -> None:
        previous = inventory.reconcile_inventory(
            [],
            [
                camera("192.168.10.20", "02:00:00:00:10:20", "uuid:camera-10"),
                camera("192.168.40.20", "02:00:00:00:40:20", "uuid:camera-40"),
            ],
            "t1",
        )

        devices = inventory.reconcile_scanned_subnets(
            previous,
            [],
            "t2",
            ["192.168.10.0/24"],
        )

        by_ip = {device["ip"]: device for device in devices}
        self.assertEqual(by_ip["192.168.10.20"]["status"], "offline")
        self.assertEqual(by_ip["192.168.10.20"]["missed_scans"], 1)
        self.assertEqual(by_ip["192.168.40.20"]["status"], "online")
        self.assertEqual(by_ip["192.168.40.20"]["last_seen"], "t1")
        self.assertEqual(by_ip["192.168.40.20"]["missed_scans"], 0)

    def test_selected_subnet_scan_tracks_stable_identity_moving_into_scope(self) -> None:
        previous = inventory.reconcile_inventory(
            [],
            [camera("192.168.40.20", "02:00:00:00:40:20", "uuid:moving-camera")],
            "t1",
        )
        candidate_uuid = previous[0]["candidate_uuid"]

        devices = inventory.reconcile_scanned_subnets(
            previous,
            [camera("192.168.10.25", "02:00:00:00:40:20", "uuid:moving-camera")],
            "t2",
            ["192.168.10.0/24"],
        )

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["candidate_uuid"], candidate_uuid)
        self.assertEqual(devices[0]["ip"], "192.168.10.25")
        self.assertEqual(devices[0]["status"], "online")

    def test_camera_reappears_online(self) -> None:
        previous = inventory.reconcile_inventory([], [camera("192.168.1.20", "aa:bb:cc:dd:ee:ff")], "t1")
        previous = inventory.reconcile_inventory(previous, [], "t2")

        devices = inventory.reconcile_inventory(
            previous,
            [camera("192.168.1.20", "aa:bb:cc:dd:ee:ff")],
            "t3",
        )

        self.assertEqual(devices[0]["status"], "online")
        self.assertEqual(devices[0]["first_seen"], "t1")
        self.assertEqual(devices[0]["candidate_uuid"], previous[0]["candidate_uuid"])
        self.assertEqual(devices[0]["last_seen"], "t3")
        self.assertEqual(devices[0]["missed_scans"], 0)

    def test_mac_tracks_camera_across_ip_change(self) -> None:
        previous = inventory.reconcile_inventory([], [camera("192.168.1.20", "aa:bb:cc:dd:ee:ff")], "t1")

        devices = inventory.reconcile_inventory(
            previous,
            [camera("192.168.1.99", "aa:bb:cc:dd:ee:ff")],
            "t2",
        )

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["ip"], "192.168.1.99")
        self.assertEqual(devices[0]["first_seen"], "t1")

    def test_different_mac_on_same_ip_is_a_new_camera(self) -> None:
        previous = inventory.reconcile_inventory([], [camera("192.168.1.20", "aa:bb:cc:dd:ee:ff")], "t1")

        devices = inventory.reconcile_inventory(
            previous,
            [camera("192.168.1.20", "11:22:33:44:55:66", "uuid:camera-2")],
            "t2",
        )

        self.assertEqual(len(devices), 2)
        self.assertEqual({device["status"] for device in devices}, {"online", "offline"})

    def test_duplicate_mac_does_not_move_existing_camera(self) -> None:
        previous = inventory.reconcile_inventory(
            [],
            [camera("192.168.1.20", "aa:bb:cc:dd:ee:ff", endpoint="")],
            "t1",
        )
        original_uuid = previous[0]["candidate_uuid"]

        devices = inventory.reconcile_inventory(
            previous,
            [
                camera("192.168.1.40", "aa:bb:cc:dd:ee:ff", endpoint=""),
                camera("192.168.1.41", "aa:bb:cc:dd:ee:ff", endpoint=""),
            ],
            "t2",
        )

        old = next(device for device in devices if device["candidate_uuid"] == original_uuid)
        self.assertEqual(old["ip"], "192.168.1.20")
        self.assertEqual(old["status"], "offline")
        self.assertEqual(
            {device["ip"] for device in devices if device["status"] == "online"},
            {"192.168.1.40", "192.168.1.41"},
        )
        conflicted = [device for device in devices if device["status"] == "online"]
        self.assertTrue(all(device["identity_conflict"] for device in conflicted))
        self.assertTrue(all("MAC" in device["identity_conflict_reason"] for device in conflicted))

    def test_unique_onvif_identity_can_disambiguate_duplicate_mac(self) -> None:
        previous = inventory.reconcile_inventory(
            [],
            [camera("192.168.1.20", "aa:bb:cc:dd:ee:ff", endpoint="uuid:camera-1")],
            "t1",
        )
        original_uuid = previous[0]["candidate_uuid"]

        devices = inventory.reconcile_inventory(
            previous,
            [
                camera("192.168.1.40", "aa:bb:cc:dd:ee:ff", endpoint="uuid:camera-1"),
                camera("192.168.1.41", "aa:bb:cc:dd:ee:ff", endpoint="uuid:camera-2"),
            ],
            "t2",
        )

        moved = next(device for device in devices if device["candidate_uuid"] == original_uuid)
        self.assertEqual(moved["ip"], "192.168.1.40")
        self.assertEqual(moved["status"], "online")

    def test_summary_counts_state(self) -> None:
        devices = [
            {**camera("192.168.1.20", "aa:bb:cc:dd:ee:ff"), "status": "online"},
            {**camera("192.168.1.21", "11:22:33:44:55:66", "uuid:camera-2"), "status": "offline"},
        ]

        summary = inventory.inventory_summary(devices)

        self.assertEqual(summary["devices"], 2)
        self.assertEqual(summary["online"], 1)
        self.assertEqual(summary["offline"], 1)

    def test_legacy_ignored_state_is_discarded_on_rediscovery(self) -> None:
        previous = inventory.reconcile_inventory(
            [], [camera("192.168.1.20", "aa:bb:cc:dd:ee:ff")], "t1"
        )
        previous[0]["ignored"] = True

        devices = inventory.reconcile_inventory(
            previous, [camera("192.168.1.20", "aa:bb:cc:dd:ee:ff")], "t2"
        )

        self.assertNotIn("ignored", devices[0])

    def test_inventory_file_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.json"
            path.write_text(json.dumps({"devices": [{"ip": "192.168.1.20"}]}), encoding="utf-8")
            with patch.object(scanner_worker, "INVENTORY", path):
                stored = scanner_worker.read_inventory()

        self.assertEqual(stored["devices"][0]["ip"], "192.168.1.20")

    def test_targeted_scan_updates_only_requested_camera(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory_path = root / "inventory.json"
            state_path = root / "state.json"
            request_path = root / "request.json"
            heartbeat_path = root / "heartbeat.json"
            previous = {
                "scan_id": "scan-before",
                "devices": [
                    {
                        **camera("192.168.1.20", "02:00:00:00:00:20", "uuid:camera-1"),
                        "candidate_uuid": "candidate-1",
                        "status": "offline",
                    },
                    {
                        **camera("192.168.1.30", "02:00:00:00:00:30", "uuid:camera-2"),
                        "candidate_uuid": "candidate-2",
                        "status": "online",
                    },
                ],
            }
            inventory_path.write_text(json.dumps(previous), encoding="utf-8")
            request_path.write_text("{}", encoding="utf-8")
            result = {
                "completed_at": "t2",
                "duration_ms": 10,
                "network": {"interface": "eth0", "subnet": "192.168.1.0/24"},
                "scanners": {"recovery": "complete"},
                "scanner_errors": {},
                "raw_log": ["targeted"],
                "devices": [camera("192.168.1.99", "02:00:00:00:00:20", "uuid:camera-1")],
            }
            with (
                patch.object(scanner_worker, "INVENTORY", inventory_path),
                patch.object(scanner_worker, "STATE", state_path),
                patch.object(scanner_worker, "REQUEST", request_path),
                patch.object(scanner_worker, "HEARTBEAT", heartbeat_path),
                patch.object(scanner_worker, "scan_targeted_lan", return_value=result),
            ):
                scanner_worker.handle_scan(
                    {
                        "scan_id": "scan-recovery",
                        "mode": "targeted",
                        "targets": [{"candidate_uuid": "candidate-1"}],
                    }
                )
                stored = json.loads(inventory_path.read_text(encoding="utf-8"))

        by_id = {device["candidate_uuid"]: device for device in stored["devices"]}
        self.assertEqual(by_id["candidate-1"]["ip"], "192.168.1.99")
        self.assertEqual(by_id["candidate-1"]["status"], "online")
        self.assertEqual(by_id["candidate-2"]["ip"], "192.168.1.30")
        self.assertEqual(by_id["candidate-2"]["status"], "online")

    def test_explicit_address_scan_preserves_unrelated_cameras(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory_path = root / "inventory.json"
            state_path = root / "state.json"
            request_path = root / "request.json"
            heartbeat_path = root / "heartbeat.json"
            previous = {
                "scan_id": "before",
                "devices": [
                    {**camera("192.168.1.20", "02:00:00:00:00:20"), "candidate_uuid": "one", "status": "online"},
                    {**camera("192.168.1.30", "02:00:00:00:00:30", "uuid:camera-2"), "candidate_uuid": "two", "status": "online"},
                ],
            }
            inventory_path.write_text(json.dumps(previous), encoding="utf-8")
            request_path.write_text("{}", encoding="utf-8")
            result = {
                "completed_at": "t2", "duration_ms": 10,
                "network": {"interface": "eth0", "subnet": "192.168.1.0/24"},
                "scanners": {"onvif": "complete", "rtsp": "complete"},
                "scanner_errors": {}, "raw_log": ["explicit"],
                "devices": [camera("192.168.1.20", "02:00:00:00:00:20")],
            }
            with (
                patch.object(scanner_worker, "INVENTORY", inventory_path),
                patch.object(scanner_worker, "STATE", state_path),
                patch.object(scanner_worker, "REQUEST", request_path),
                patch.object(scanner_worker, "HEARTBEAT", heartbeat_path),
                patch.object(scanner_worker, "scan_explicit_address", return_value=result),
            ):
                scanner_worker.handle_scan(
                    {"scan_id": "explicit", "mode": "address", "address": "192.168.1.20"}
                )
                stored = json.loads(inventory_path.read_text(encoding="utf-8"))

        by_id = {device["candidate_uuid"]: device for device in stored["devices"]}
        self.assertEqual(by_id["two"]["status"], "online")

    def test_scan_progress_tracks_each_subnet_independently(self) -> None:
        first = scanner_worker.LanInterface(
            "eth0",
            ipaddress.IPv4Address("192.168.40.2"),
            ipaddress.IPv4Network("192.168.40.0/24"),
        )
        second = scanner_worker.LanInterface(
            "eth0",
            ipaddress.IPv4Address("192.168.40.2"),
            ipaddress.IPv4Network("10.0.202.0/24"),
            directly_connected=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            heartbeat_path = root / "heartbeat.json"
            state_path.write_text(
                json.dumps(
                    {
                        "networks": [
                            {"subnet": "192.168.40.0/24", "status": "queued"},
                            {"subnet": "10.0.202.0/24", "status": "queued"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(scanner_worker, "STATE", state_path),
                patch.object(scanner_worker, "HEARTBEAT", heartbeat_path),
            ):
                scanner_worker.scan_progress("onvif", "running", first)
                scanner_worker.scan_progress("onvif", "running", second)
                scanner_worker.scan_progress("onvif", "complete", first)
                state = json.loads(state_path.read_text(encoding="utf-8"))

        networks = {network["subnet"]: network for network in state["networks"]}
        self.assertEqual(networks["192.168.40.0/24"]["status"], "complete")
        self.assertEqual(networks["10.0.202.0/24"]["status"], "running")
        self.assertEqual(state["scanners"]["onvif"], "running")


if __name__ == "__main__":
    unittest.main()
