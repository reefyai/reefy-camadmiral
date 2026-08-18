from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DiscoveryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "camadmiral" / "index.html").read_text(encoding="utf-8")

    def test_manual_camera_entry_accepts_ip_or_rtsp_url(self) -> None:
        self.assertIn("Add camera manually", self.html)
        self.assertIn("Camera IP or complete RTSP URL", self.html)
        self.assertIn("manualCameraTarget", self.html)

    def test_pasted_rtsp_credentials_are_removed_from_visible_source(self) -> None:
        self.assertIn('source.username = ""', self.html)
        self.assertIn('source.password = ""', self.html)
        self.assertIn("addressInput.value = target.draft.source", self.html)

    def test_discovery_has_no_persistent_ignore_controls(self) -> None:
        self.assertNotIn('id="show-ignored"', self.html)
        self.assertNotIn("setCandidateIgnored", self.html)

    def test_phone_layout_replaces_wide_rows_with_compact_cards(self) -> None:
        self.assertIn("tbody tr.camera-row", self.html)
        self.assertIn('grid-template-areas: "identity action" "status action"', self.html)
        self.assertIn("camera-mobile-ip", self.html)
        self.assertIn("thead { display: none; }", self.html)

    def test_phone_camera_name_editor_does_not_use_vertical_flex_basis(self) -> None:
        self.assertIn(".camera-management .setup-field { flex: none;", self.html)
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto auto", self.html)

    def test_camera_rows_are_fixed_and_long_values_are_clipped(self) -> None:
        self.assertIn(".camera-row { height: 68px; }", self.html)
        self.assertIn("text-overflow: ellipsis", self.html)
        self.assertIn("node.title = value", self.html)
        self.assertIn('row.className = `camera-row ${status.key}`', self.html)

    def test_camera_details_render_in_a_modal_not_in_table_rows(self) -> None:
        self.assertIn('id="app-modal" role="dialog"', self.html)
        self.assertIn('openAppModal("camera"', self.html)
        self.assertNotIn("detail-row", self.html)
        self.assertNotIn("detailCell.colSpan", self.html)

    def test_popups_share_modal_shell_and_disable_uses_custom_confirmation(self) -> None:
        self.assertGreaterEqual(self.html.count('class="modal-backdrop'), 3)
        self.assertIn('openAppModal("manual"', self.html)
        self.assertIn('openAppModal("scan"', self.html)
        self.assertIn('id="confirm-modal" role="alertdialog"', self.html)
        self.assertNotIn("window.confirm", self.html)

    def test_live_view_is_a_fast_table_action(self) -> None:
        self.assertIn('addText(actionStack, "button", "row-action", "Live")', self.html)
        self.assertIn('showLiveView(device.adoption.camera_uuid, device.display_name)', self.html)
        self.assertNotIn("function addLiveViewAction", self.html)

    def test_camera_details_are_grouped_into_compact_sections(self) -> None:
        self.assertIn("addCameraManagement(summary, device, adoption)", self.html)
        self.assertNotIn('addDetailSection(wrapper, "Camera settings")', self.html)
        self.assertIn('addDetailSection(wrapper, "Streams", streamCount)', self.html)
        self.assertIn('addDetailSection(wrapper, "Integrations"', self.html)
        self.assertIn("connection-details", self.html)
        self.assertNotIn("camera-facts", self.html)

    def test_frigate_status_explains_automatic_retry(self) -> None:
        self.assertIn("Waiting for camera process", self.html)
        self.assertIn("CamAdmiral will retry until its process appears.", self.html)
        self.assertNotIn("Retry pending", self.html)

    def test_camera_details_show_bounded_availability_views(self) -> None:
        self.assertIn('addDetailSection(parent, "Availability"', self.html)
        self.assertIn('[["24h", "24 hours"], ["7d", "7 days"]]', self.html)
        self.assertIn("availability-block", self.html)
        self.assertIn("availability_percent", self.html)
        self.assertIn("Unknown or disabled", self.html)
        self.assertIn("cursor: default", self.html)

    def test_runtime_health_uses_role_streams_without_ambiguous_table_idle(self) -> None:
        self.assertIn("const roleStreams = new Set", self.html)
        self.assertIn('label: "not observed"', self.html)
        self.assertNotIn('label: "idle"', self.html)
        self.assertIn("Idle - waiting for a consumer", self.html)

    def test_camera_rows_show_compact_recent_availability(self) -> None:
        self.assertIn("ROW_AVAILABILITY_BLOCKS = 8", self.html)
        self.assertIn("row-availability-strip", self.html)
        self.assertIn("addRowAvailability(statusStack, device)", self.html)
        self.assertIn("Recent camera availability", self.html)
        self.assertIn(".row-availability-block:nth-child(-n+2)", self.html)

    def test_incidents_and_telegram_settings_use_compact_shared_modals(self) -> None:
        self.assertIn('id="show-incidents"', self.html)
        self.assertIn('id="incident-count"', self.html)
        self.assertIn('openAppModal("incidents"', self.html)
        self.assertIn('id="show-notifications"', self.html)
        self.assertIn('openAppModal("notifications"', self.html)
        self.assertIn("Paste token from BotFather", self.html)
        self.assertIn("Open Telegram", self.html)
        self.assertIn('autocomplete = "off"', self.html)
        self.assertNotIn('"Telegram alerts"', self.html)
        self.assertNotIn("enabled.checked", self.html)
        self.assertIn("const body = {enabled: true}", self.html)


if __name__ == "__main__":
    unittest.main()
