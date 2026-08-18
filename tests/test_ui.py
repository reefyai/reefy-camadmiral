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

    def test_header_and_browser_tab_use_the_app_icon(self) -> None:
        self.assertIn('<link rel="icon" type="image/png" href="/app-icon.png">', self.html)
        self.assertIn('<link rel="apple-touch-icon" href="/app-icon.png">', self.html)
        self.assertIn('class="app-brand"', self.html)
        self.assertIn('<img src="/app-icon.png" alt=""', self.html)

    def test_header_actions_are_grouped_into_compact_utility_and_scan_controls(self) -> None:
        self.assertIn('class="utility-actions"', self.html)
        self.assertIn('class="scan-actions"', self.html)
        self.assertIn('id="show-add-address" type="button" aria-haspopup="dialog">Add camera</button>', self.html)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", self.html)

    def test_pasted_rtsp_credentials_are_removed_from_visible_source(self) -> None:
        self.assertIn('source.username = ""', self.html)
        self.assertIn('source.password = ""', self.html)
        self.assertIn("addressInput.value = target.draft.source", self.html)

    def test_discovery_has_no_persistent_ignore_controls(self) -> None:
        self.assertNotIn('id="show-ignored"', self.html)
        self.assertNotIn("setCandidateIgnored", self.html)

    def test_phone_layout_replaces_wide_rows_with_compact_cards(self) -> None:
        self.assertIn("tbody tr.camera-row", self.html)
        self.assertIn('grid-template-areas: "preview identity action" "preview status action"', self.html)
        self.assertIn("camera-mobile-ip", self.html)
        self.assertIn(".preview-cell { width: 78px; height: 58px; }", self.html)
        self.assertIn(".camera-model { display: none; }", self.html)
        self.assertIn("thead { display: none; }", self.html)

    def test_phone_camera_name_editor_does_not_use_vertical_flex_basis(self) -> None:
        self.assertIn(".camera-management .setup-field { flex: none;", self.html)
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto auto", self.html)

    def test_camera_rows_are_fixed_and_long_values_are_clipped(self) -> None:
        self.assertIn(".camera-row { height: 76px; }", self.html)
        self.assertIn("text-overflow: ellipsis", self.html)
        self.assertIn("node.title = value", self.html)
        self.assertIn('row.className = `camera-row ${status.key}`', self.html)

    def test_camera_details_render_in_a_modal_not_in_table_rows(self) -> None:
        self.assertIn('id="app-modal" role="dialog"', self.html)
        self.assertIn('openAppModal("camera"', self.html)
        self.assertNotIn("detail-row", self.html)
        self.assertNotIn("detailCell.colSpan", self.html)

    def test_connectivity_is_one_compact_stacked_column(self) -> None:
        self.assertIn("<th>Connectivity</th>", self.html)
        self.assertNotIn('data-sort="ip"', self.html)
        self.assertNotIn('data-sort="mac"', self.html)
        self.assertNotIn('data-sort="onvif"', self.html)
        self.assertNotIn('data-sort="rtsp"', self.html)
        self.assertIn('"connectivity-cell"', self.html)
        self.assertIn('["IP", device.ip || "-", false]', self.html)
        self.assertIn('["MAC", device.mac || "-", false]', self.html)
        self.assertIn('["ONVIF", device.onvif?.service_urls?.length ? "Available"', self.html)
        self.assertIn('["RTSP", device.rtsp?.length ? "Available"', self.html)

    def test_table_recent_view_uses_only_the_cached_thumbnail_endpoint(self) -> None:
        self.assertIn("<th>Recent view</th>", self.html)
        self.assertIn('"preview-cell"', self.html)
        self.assertIn("/thumbnail.jpg`;", self.html)
        self.assertNotIn("/snapshot.jpg`;\n          previewCell", self.html)
        self.assertIn('image.loading = "lazy"', self.html)
        self.assertNotIn("image.hidden = true", self.html)
        self.assertIn('image.classList.add("ready")', self.html)
        self.assertIn('const livePreview = Boolean(device.adoption?.camera_uuid', self.html)
        self.assertIn('addText(preview, livePreview ? "button" : "div", "preview-cell", "")', self.html)
        self.assertIn('previewCell.addEventListener("click", () => showLiveView', self.html)

    def test_unadopted_cameras_have_separate_details_and_adopt_actions(self) -> None:
        self.assertIn('addText(actionStack, "button", "row-action", "Details")', self.html)
        self.assertIn('addText(actionStack, "button", "row-action", "Adopt")', self.html)
        self.assertIn("function discoveredCameraDetails(device)", self.html)
        self.assertIn("async function openCameraAdoption(device, trigger = null)", self.html)
        self.assertIn('openAppModal("adopt"', self.html)
        self.assertIn('? adoptionDetails(device)', self.html)

    def test_adoption_modal_is_compact_and_owns_camera_inspection(self) -> None:
        self.assertIn('openAppModal("adopt", `Adopt ${device.display_name || device.ip}`, adoptionDetails(device), trigger, "compact")', self.html)
        self.assertIn('addAdoptionProgress(wrapper, "Checking camera connectivity...")', self.html)
        self.assertIn('"Enter the camera credentials to continue."', self.html)
        self.assertNotIn('const connect = addDetailSection(wrapper, "Adopt camera")', self.html)

    def test_adopted_camera_details_use_saved_data_while_offline(self) -> None:
        self.assertIn("function savedAdoptionInspection(adoption)", self.html)
        self.assertIn("const adoption = device.adoption || result?.adoption", self.html)
        self.assertIn('result = savedAdoptionInspection(adoption)', self.html)
        self.assertIn("device.adoption ? inspectionDetails(device) : discoveredCameraDetails(device)", self.html)

    def test_popups_share_modal_shell_and_disable_uses_custom_confirmation(self) -> None:
        self.assertGreaterEqual(self.html.count('class="modal-backdrop'), 3)
        self.assertIn('openAppModal("manual"', self.html)
        self.assertIn('openAppModal("scan"', self.html)
        self.assertIn('id="confirm-modal" role="alertdialog"', self.html)
        self.assertNotIn("window.confirm", self.html)

    def test_live_view_is_a_fast_table_action(self) -> None:
        self.assertIn('addText(actionStack, "button", "row-action", "Live")', self.html)
        self.assertIn('showLiveView(device.adoption.camera_uuid, device.display_name)', self.html)
        self.assertLess(
            self.html.index('addText(actionStack, "button", "row-action", "Details")'),
            self.html.index('addText(actionStack, "button", "row-action", "Live")'),
        )
        self.assertNotIn("function addLiveViewAction", self.html)

    def test_live_view_has_no_jumping_native_timeline(self) -> None:
        self.assertIn('id="live-video" aria-label="Live camera view" autoplay muted playsinline>', self.html)
        self.assertNotIn('playsinline controls', self.html)
        self.assertIn('id="live-fullscreen"', self.html)
        self.assertIn("frame.requestFullscreen()", self.html)

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
        self.assertIn('id="availability-tooltip" role="tooltip"', self.html)
        self.assertIn("showAvailabilityTooltip(block)", self.html)
        self.assertIn("toggleAvailabilityTooltip(block)", self.html)
        self.assertIn("applyAvailabilitySegments(block, bucket)", self.html)
        self.assertIn("availabilitySegmentSummary(bucket)", self.html)
        self.assertIn("Ended ${availabilityState(bucket.state).toLowerCase()}", self.html)
        self.assertNotIn("block.title = description", self.html)

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
        self.assertIn('addText(parent, "button", `${className} ${bucket.state}`', self.html)
        self.assertIn("row-availability-block.selected", self.html)

    def test_health_uses_green_while_brand_controls_keep_teal(self) -> None:
        self.assertIn(".device-status.online, .device-status.healthy { color: #86efac; }", self.html)
        self.assertIn("background: #22c55e", self.html)
        self.assertIn("#scan {", self.html)
        self.assertIn("background: #2dd4bf", self.html)

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
