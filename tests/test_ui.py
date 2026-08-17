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
        self.assertIn(".camera-management .setup-field { grid-column: 1; flex: none;", self.html)
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto", self.html)

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
        self.assertIn('addDetailSection(wrapper, "Camera settings")', self.html)
        self.assertIn('addDetailSection(wrapper, "Streams", streamCount)', self.html)
        self.assertIn('addDetailSection(wrapper, "Integrations"', self.html)
        self.assertIn("connection-details", self.html)
        self.assertNotIn("camera-facts", self.html)

    def test_frigate_status_explains_automatic_retry(self) -> None:
        self.assertIn("Waiting for camera process", self.html)
        self.assertIn("CamAdmiral will retry until its process appears.", self.html)
        self.assertNotIn("Retry pending", self.html)


if __name__ == "__main__":
    unittest.main()
