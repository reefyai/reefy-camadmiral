from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DiscoveryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "camadmiral" / "index.html").read_text(encoding="utf-8")

    def test_inline_javascript_parses(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is not installed")
        script = self.html.split("<script>", 1)[1].split("</script>", 1)[0]
        completed = subprocess.run(
            [node, "--check", "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_manual_camera_entry_accepts_ip_or_rtsp_url(self) -> None:
        self.assertIn("Add camera manually", self.html)
        self.assertIn("Camera IP or complete RTSP URL", self.html)
        self.assertIn("manualCameraTarget", self.html)

    def test_header_and_browser_tab_use_the_app_icon(self) -> None:
        self.assertIn('<link rel="icon" type="image/png" href="/app-icon.png">', self.html)
        self.assertIn('<link rel="apple-touch-icon" href="/app-icon.png">', self.html)
        self.assertIn('class="app-brand"', self.html)
        self.assertIn('class="app-brand" href="/" aria-label="CamAdmiral dashboard"', self.html)
        self.assertIn('<img src="/app-icon.png" alt=""', self.html)

    def test_dashboard_scan_action_expands_inline_status_and_diagnostics(self) -> None:
        self.assertIn('class="primary-nav"', self.html)
        self.assertIn('class="dashboard-actions"', self.html)
        self.assertNotIn('class="dashboard-controls"', self.html)
        self.assertNotIn('class="scan-status"', self.html)
        self.assertNotIn('<h2 class="dashboard-title">Cameras</h2>', self.html)
        self.assertIn('id="show-add-address" type="button" aria-controls="manual-card" aria-expanded="false">Add camera</button>', self.html)
        self.assertIn('<button id="scan" type="button" aria-controls="scan-card" aria-expanded="false">Scan network</button>', self.html)
        self.assertIn('<button id="scan-start" type="button">Scan network</button>', self.html)
        self.assertEqual(self.html.count('id="error"'), 1)
        self.assertIn('id="scan-card" aria-labelledby="scan-card-title" hidden', self.html)
        self.assertIn('id="manual-card" aria-labelledby="manual-card-title" hidden', self.html)
        self.assertNotIn('openAppModal("scan"', self.html)
        self.assertNotIn('openAppModal("manual"', self.html)
        self.assertIn('scanStartButton.addEventListener("click", async () =>', self.html)
        self.assertIn('function scanStatusLabel(data)', self.html)
        self.assertNotIn('Last scan:', self.html)
        self.assertNotIn('complete: "Scan complete"', self.html)
        self.assertIn(".primary-nav a[aria-current=\"page\"]", self.html)
        self.assertIn("justify-content: flex-end; gap: 8px; margin-bottom: 16px", self.html)
        self.assertIn("width: 120px; min-height: 38px", self.html)
        self.assertIn('id="scan-network-list"', self.html)
        self.assertIn('id="scan-network-input"', self.html)
        self.assertIn('id="scan-network-add-toggle"', self.html)
        self.assertIn('aria-label="Add subnet"', self.html)
        self.assertIn('id="scan-network-add" hidden', self.html)
        self.assertNotIn('id="scan-networks-save"', self.html)
        self.assertNotIn('id="scan-networks-title"', self.html)
        self.assertNotIn('Custom private subnets can include', self.html)
        self.assertIn('id="scan-results" aria-labelledby="scan-results-title" hidden', self.html)
        self.assertIn('id="scan-run-status" hidden', self.html)
        self.assertIn('resetScanPresentation()', self.html)
        self.assertIn('scanPanelScanId = activeState?.scan_id || null', self.html)
        self.assertIn('fetch("/internal/discovery/networks"', self.html)
        self.assertIn('"X-CamAdmiral-Action": "save-discovery-networks"', self.html)
        self.assertIn('<details class="scan-log-details" id="scan-log-details">', self.html)
        self.assertIn('<summary>Logs <span id="scan-log-count"></span></summary>', self.html)
        self.assertNotIn('<details class="scan-log-details" id="scan-log-details" open>', self.html)
        self.assertNotIn('excluded', self.html.lower())
        self.assertNotIn('Custom · routed unicast', self.html)
        self.assertNotIn('"Scan again"', self.html)
        self.assertNotIn('Restore detected subnet', self.html)
        self.assertIn('checkbox.type = "checkbox"', self.html)
        self.assertIn('checkbox.checked = selectedDiscoverySubnets.has(cidr)', self.html)
        self.assertIn('Auto-discovered on ${network.interface || "network interface"}', self.html)
        self.assertIn('"Custom added"', self.html)
        self.assertIn('if (network.source === "custom")', self.html)
        self.assertIn('scanNetworkAddToggle.addEventListener("click", () =>', self.html)
        self.assertLess(self.html.index('id="scan-network-list"'), self.html.index('id="scan-network-add-toggle"'))
        self.assertIn('await saveDiscoveryNetworks(previousConfiguration, previousSubnets)', self.html)
        self.assertIn('custom_subnets: discoveryNetworkConfiguration', self.html)
        self.assertIn('.scan-action #scan-start { min-height: 40px; }', self.html)
        self.assertIn('id="scan-network-progress"', self.html)
        self.assertIn('function renderScanNetworkProgress(networks = [])', self.html)
        self.assertIn('scanResults.hidden = false', self.html)
        self.assertIn('setText(stateLabel, "Scan complete")', self.html)
        self.assertIn('const busyChanged = busy !== active', self.html)
        self.assertIn('if (busyChanged && discoveryNetworkConfiguration.length) renderDiscoveryNetworks()', self.html)
        self.assertNotIn('id="scanner-progress"', self.html)

    def test_custom_subnet_is_validated_before_it_enters_the_list(self) -> None:
        self.assertIn('function privateIpv4Cidr(value)', self.html)
        self.assertIn('usableHosts > maxCustomScanHosts', self.html)
        self.assertIn('Custom subnets are limited to ${maxCustomScanHosts.toLocaleString()}.', self.html)
        self.assertIn('cidr = privateIpv4Cidr(scanNetworkInput.value)', self.html)

    def test_frigate_full_sync_failure_shows_stage_resource_and_code(self) -> None:
        self.assertIn("function frigateFailureDetail(result, fallback)", self.html)
        self.assertIn('parts.push(`Stage: ${stages[result.stage] || result.stage}.`)', self.html)
        self.assertIn('parts.push(`Resource: ${result.resource}.`)', self.html)
        self.assertIn('parts.push(`Code: ${result.status}.`)', self.html)
        self.assertIn('verify_stream_configuration: "verify stale stream configuration"', self.html)
        self.assertIn('verify_runtime_cleanup: "verify stale live streams"', self.html)
        self.assertIn('"frigate-target-error"', self.html)
        self.assertIn("setText(errorDetail, error.message)", self.html)

    def test_pasted_rtsp_credentials_are_removed_from_visible_source(self) -> None:
        self.assertIn('source.username = ""', self.html)
        self.assertIn('source.password = ""', self.html)
        self.assertIn("addressInput.value = target.draft.source", self.html)

    def test_discovery_has_no_persistent_ignore_controls(self) -> None:
        self.assertNotIn('id="show-ignored"', self.html)
        self.assertNotIn("setCandidateIgnored", self.html)

    def test_phone_layout_replaces_wide_rows_with_compact_cards(self) -> None:
        self.assertIn("tbody tr.camera-row", self.html)
        self.assertIn('grid-template-areas: "preview identity" "preview status" "action action"', self.html)
        self.assertIn("grid-template-rows: 40px 30px 72px", self.html)
        self.assertIn("height: 190px; min-height: 190px; max-height: 190px", self.html)
        self.assertIn("grid-template-columns: 1fr 1fr", self.html)
        self.assertIn(".action-stack .row-action { min-width: 0; min-height: 32px", self.html)
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

    def test_desktop_camera_columns_size_from_their_content(self) -> None:
        self.assertIn("table-layout: auto", self.html)
        self.assertNotIn("<colgroup>", self.html)

    def test_summary_counts_filter_camera_rows(self) -> None:
        self.assertIn('class="camera-list-surface" aria-label="Camera list"', self.html)
        self.assertIn(".camera-list-surface {", self.html)
        self.assertIn("padding: 12px 14px; border-bottom:", self.html)
        self.assertIn('data-camera-filter="all"', self.html)
        self.assertIn('data-camera-filter="online"', self.html)
        self.assertIn('data-camera-filter="offline"', self.html)
        self.assertIn('data-camera-filter="blocked"', self.html)
        self.assertIn("let cameraFilter = null", self.html)
        self.assertIn("function cameraConnectivity(device)", self.html)
        self.assertIn("const visibleDevices = devices.filter(device => !device.blocked)", self.html)
        self.assertIn('if (cameraFilter === "blocked") return Boolean(device.blocked)', self.html)
        self.assertIn("return !cameraFilter || cameraConnectivity(device) === cameraFilter", self.html)
        self.assertNotIn("!cameraFilter || device.status === cameraFilter", self.html)
        self.assertIn("updateSummaryCounts();", self.html)
        self.assertIn('cameraFilter = requested === "all" || cameraFilter === requested ? null : requested', self.html)
        self.assertIn('button.classList.toggle("selected", selected)', self.html)

    def test_camera_lifecycle_actions_are_grouped_without_hiding_primary_actions(self) -> None:
        self.assertIn('addText(actionStack, "button", "row-action", "Details")', self.html)
        self.assertIn('addText(actionStack, "button", "row-action", "Streams")', self.html)
        self.assertIn('addText(actionStack, "button", "row-action overflow-action", "⋯")', self.html)
        self.assertIn('id="camera-action-menu" role="menu"', self.html)
        self.assertIn('more.setAttribute("aria-haspopup", "menu")', self.html)
        self.assertNotIn('openAppModal("actions"', self.html)
        self.assertIn('addCameraMenuAction("Unadopt"', self.html)
        self.assertIn('headers: {"X-CamAdmiral-Action": "unadopt-camera"}', self.html)
        self.assertIn('addCameraMenuAction("Block device"', self.html)
        self.assertIn('headers: {"X-CamAdmiral-Action": "block-camera"}', self.html)
        self.assertIn('addText(actionStack, "button", "row-action", "Unblock")', self.html)

    def test_new_camera_disable_action_is_not_offered(self) -> None:
        self.assertNotIn('"Disable camera"', self.html)
        self.assertIn('addCameraMenuAction("Enable camera"', self.html)
        self.assertIn("disabled by an earlier CamAdmiral version", self.html)

    def test_empty_frigate_target_guides_user_to_camera_streams(self) -> None:
        self.assertIn("No CamAdmiral cameras synced yet.", self.html)
        self.assertIn('addText(actions, "button", "row-action", "View cameras")', self.html)
        self.assertIn('window.location.href = "/"', self.html)

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
        self.assertIn('"connectivity-protocols"', self.html)
        self.assertIn('addText(protocols, "span", "protocol-badge", "ONVIF")', self.html)
        self.assertIn('addText(protocols, "span", "protocol-badge", "RTSP")', self.html)
        self.assertNotIn('["ONVIF", device.onvif?.service_urls?.length ? "Available"', self.html)
        self.assertNotIn('["RTSP", device.rtsp?.length ? "Available"', self.html)

    def test_table_recent_view_uses_only_the_cached_thumbnail_endpoint(self) -> None:
        self.assertIn("<th>Recent view</th>", self.html)
        self.assertIn('"preview-cell"', self.html)
        self.assertIn("/thumbnail.jpg?captured=", self.html)
        self.assertIn("device.adoption?.camera_uuid && device.adoption.thumbnail_captured_at", self.html)
        self.assertIn("device.adoption?.thumbnail_captured_at", self.html)
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

    def test_detail_popups_share_modal_shell_and_disable_uses_custom_confirmation(self) -> None:
        self.assertGreaterEqual(self.html.count('class="modal-backdrop'), 3)
        self.assertIn('openAppModal("camera"', self.html)
        self.assertIn('openAppModal("streams"', self.html)
        self.assertNotIn('openAppModal("manual"', self.html)
        self.assertNotIn('openAppModal("scan"', self.html)
        self.assertIn('id="confirm-modal" role="alertdialog"', self.html)
        self.assertNotIn("window.confirm", self.html)

    def test_live_view_opens_from_the_recent_screenshot_without_a_duplicate_action(self) -> None:
        self.assertNotIn('addText(actionStack, "button", "row-action", "Live")', self.html)
        self.assertIn('previewCell.addEventListener("click", () => showLiveView', self.html)
        self.assertNotIn("function addLiveViewAction", self.html)

    def test_streams_have_a_separate_table_action_and_modal(self) -> None:
        self.assertIn('addText(actionStack, "button", "row-action", "Streams")', self.html)
        self.assertIn("function openCameraStreams(device, trigger = null)", self.html)
        self.assertIn('openAppModal("streams", title, streamDetails(device), trigger, "wide")', self.html)
        self.assertIn('appModalKind === "streams"', self.html)
        self.assertIn("function streamDetails(device)", self.html)
        self.assertIn("addAvailability(wrapper, device, adoption)", self.html)

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

    def test_downstream_urls_are_selectable_single_line_scrollers(self) -> None:
        self.assertIn('const downstreamPasswordMask = "********"', self.html)
        self.assertIn('function downstreamUrl(streamKey, maskPassword = false, addressMode = "lan")', self.html)
        self.assertIn('addressMode === "localhost" ? "localhost" : mediaHost', self.html)
        self.assertIn("downstreamUrl(managed.stream_key, true, addressMode)", self.html)
        self.assertIn(': displayUrl || (managed ?', self.html)
        self.assertIn('const urlText = addText(row, "div", "downstream-url", value)', self.html)
        self.assertIn("urlText.title = displayUrl", self.html)
        self.assertIn('copy.addEventListener("click", () => copyText(url, copy))', self.html)
        self.assertNotIn("urlText.title = url", self.html)
        self.assertNotIn('`Downstream URL: ${url}`', self.html)
        self.assertIn("urlText.tabIndex = 0", self.html)
        self.assertIn(".stream-access { grid-column: 2; grid-row: 1 / span 2; min-width: 0;", self.html)
        self.assertIn("overflow-x: auto; overflow-y: hidden", self.html)
        self.assertIn("user-select: text; white-space: nowrap", self.html)

    def test_camera_source_and_health_are_grouped_under_stream_specs(self) -> None:
        self.assertIn('const metadata = addText(row, "div", "profile-metadata", "")', self.html)
        self.assertIn('addText(metadata, "div", "profile-specs", specifications)', self.html)
        self.assertIn("metadata.append(source)", self.html)
        self.assertIn('addText(metadata, "div", `media-state', self.html)
        self.assertNotIn('addText(endpoint, "div", `media-state', self.html)

    def test_stream_rows_keep_metadata_clear_of_long_profile_names(self) -> None:
        self.assertIn("grid-template-columns: minmax(260px, .9fr) minmax(360px, 1.6fr)", self.html)
        self.assertIn(".profile-name { color: #e0eaf0; font-weight: 700; overflow-wrap: anywhere; }", self.html)
        self.assertIn(".stream-access { grid-column: 2; grid-row: 1 / span 2;", self.html)
        self.assertIn(".stream-identity, .profile-metadata, .stream-access { grid-column: 1; grid-row: auto; }", self.html)

    def test_frigate_status_explains_automatic_retry(self) -> None:
        self.assertIn("function frigateSyncError(errorCode)", self.html)
        self.assertIn("Waiting for camera process", self.html)
        self.assertIn("CamAdmiral will retry until its process appears.", self.html)
        self.assertIn("Camera configuration missing", self.html)
        self.assertIn("Detection settings differ", self.html)
        self.assertIn("Runtime stream missing", self.html)
        self.assertIn("[stateLabel, statusHelp] = frigateSyncError(target.error_code)", self.html)
        self.assertIn('"integration-help frigate-camera-help", statusHelp', self.html)
        self.assertIn("Error code: ${errorCode}.", self.html)
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

    def test_open_camera_availability_refreshes_without_page_reload(self) -> None:
        self.assertIn("CAMERA_AVAILABILITY_REFRESH_MS = 30000", self.html)
        self.assertIn("function refreshOpenCameraAvailability()", self.html)
        self.assertIn("setInterval(refreshOpenCameraAvailability, CAMERA_AVAILABILITY_REFRESH_MS)", self.html)
        self.assertIn("loadedAt: Date.now()", self.html)

    def test_camera_name_does_not_repeat_adoption_state(self) -> None:
        self.assertNotIn('"Adopted · Disabled" : "Adopted"', self.html)
        self.assertNotIn("adopted-label", self.html)

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

    def test_primary_navigation_and_settings_subpages_are_persistent(self) -> None:
        self.assertIn('id="nav-dashboard" href="/">Dashboard</a>', self.html)
        self.assertIn('id="nav-incidents" href="/incidents">Incidents', self.html)
        self.assertIn('id="nav-settings" href="/settings/notifications">Settings</a>', self.html)
        self.assertIn('id="incident-count"', self.html)
        self.assertIn('id="incidents-view" hidden', self.html)
        self.assertIn('id="settings-view" hidden', self.html)
        self.assertIn('href="/settings/notifications">Notifications</a>', self.html)
        self.assertIn('href="/settings/integrations">Integrations</a>', self.html)
        self.assertIn('id="telegram-settings-body"', self.html)
        self.assertIn('id="frigate-targets"', self.html)
        self.assertIn('openAppModal("frigate"', self.html)
        self.assertIn('"http://127.0.0.1:5000"', self.html)
        self.assertIn('"http://frigate.local:5000"', self.html)
        self.assertIn("CamAdmiral will make privileged Frigate API requests to this URL.", self.html)
        self.assertNotIn("Sync adopted cameras", self.html)
        self.assertNotIn("sync_cameras", self.html)
        self.assertNotIn('id="show-notifications"', self.html)
        self.assertIn("Paste token from BotFather", self.html)
        self.assertIn("Open Telegram", self.html)
        self.assertIn('autocomplete = "off"', self.html)
        self.assertNotIn('"Telegram alerts"', self.html)
        self.assertNotIn("enabled.checked", self.html)
        self.assertIn("const body = {enabled: true}", self.html)

    def test_frigate_full_sync_is_one_confirmed_target_action(self) -> None:
        self.assertIn('"Full sync now"', self.html)
        self.assertIn('/full-sync`, {cache: "no-store"}', self.html)
        self.assertIn('"X-CamAdmiral-Action": "full-sync-frigate-target"', self.html)
        self.assertIn("Other Frigate cameras and streams will not be changed.", self.html)

    def test_camera_actions_offer_per_target_sync_and_masked_config_preview(self) -> None:
        self.assertIn('"Frigate destinations"', self.html)
        self.assertIn("frigate.append(frigateSyncDetails(device, addressMode))", self.html)
        self.assertNotIn("function openCameraSync", self.html)
        self.assertIn('"X-CamAdmiral-Action": "sync-frigate-camera"', self.html)
        self.assertIn('"X-CamAdmiral-Action": "remove-frigate-camera"', self.html)
        self.assertIn("CamAdmiral will not restart Frigate", self.html)
        self.assertIn("Restart required", self.html)
        self.assertIn("Restart Frigate when convenient, then click Test", self.html)
        self.assertIn("display_configuration", self.html)
        self.assertIn("Copy configuration", self.html)
        self.assertIn("Copy includes the working plaintext credential.", self.html)
        self.assertIn(".config-preview-note { margin: 10px 0 0; color: #f59e0b;", self.html)
        self.assertIn('[["lan", "LAN"], ["localhost", "Localhost"]]', self.html)
        self.assertIn('radio.type = "radio"', self.html)
        self.assertIn("streamAddressModes.set(cameraUuid, value)", self.html)
        self.assertIn('"X-CamAdmiral-Action": "set-camera-stream-address"', self.html)
        self.assertIn("adoption.stream_address_mode || \"lan\"", self.html)
        self.assertNotIn("Localhost works only when Frigate shares the host network.", self.html)
        self.assertIn("JSON.stringify({address_mode: addressMode})", self.html)
        self.assertNotIn("addressSelect", self.html)

    def test_camera_sync_polls_with_a_spinner_until_final_status(self) -> None:
        sync_handler = self.html.split('"X-CamAdmiral-Action": "sync-frigate-camera"', 1)[1]
        sync_handler = sync_handler.split('const showConfig =', 1)[0]
        self.assertIn("await waitForFrigateCameraSync", sync_handler)
        self.assertIn("frigateSyncProgress.set(syncKey, true)", self.html)
        self.assertIn("frigateSyncProgress.delete(syncKey)", sync_handler)
        self.assertIn("renderActiveCameraModal();", sync_handler)
        self.assertIn('addText(sync, "span", "inline-spinner", "")', self.html)
        self.assertIn('sync.classList.add("syncing-action")', self.html)
        self.assertIn(".syncing-action { display: inline-flex; align-items: center; gap: 6px; }", self.html)
        self.assertIn("display: inline-block; box-sizing: border-box; width: 14px; height: 14px", self.html)
        self.assertIn("async function waitForFrigateCameraSync", self.html)
        self.assertIn('status?.status === "applied" || status?.status === "error"', self.html)
        self.assertNotIn("closeAppModal", sync_handler)
        self.assertIn('["camera", "streams", "adopt"]', self.html)
        self.assertNotIn('appModalKind === "frigate-sync"', self.html)


if __name__ == "__main__":
    unittest.main()
