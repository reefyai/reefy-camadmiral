from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "e2e-artifacts"
BASE_URL = os.environ.get("CAMADMIRAL_E2E_WEB_URL", "http://127.0.0.1:18089")
ADMIN_PASSWORD = os.environ.get(
    "CAMADMIRAL_E2E_ADMIN_PASSWORD",
    "synthetic-e2e-admin-password",
)


class UiScenarioFailure(RuntimeError):
    pass


def assert_mobile_camera_actions(page: Page) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.locator("#camera-rows tr.camera-row").nth(2).wait_for(
        state="attached", timeout=30_000
    )

    action_layout = page.locator(".dashboard-actions").evaluate(
        """
        controls => {
          const scan = controls.querySelector("#scan").getBoundingClientRect();
          const add = controls.querySelector("#show-add-address").getBoundingClientRect();
          return {
            sameRow: Math.abs(scan.top - add.top) < 1,
            primaryLast: add.left < scan.left,
            touchTargets: scan.height >= 44 && add.height >= 44,
            equalSize: Math.abs(scan.width - add.width) < 1 && Math.abs(scan.height - add.height) < 1,
            insideViewport: scan.left >= 0 && add.right <= window.innerWidth + 0.5,
          };
        }
        """
    )
    failed_rules = [name for name, passed in action_layout.items() if not passed]
    if failed_rules:
        raise UiScenarioFailure(
            f"Mobile dashboard action bar failed: {', '.join(failed_rules)}"
        )
    if page.get_by_role("heading", name="Cameras").count():
        raise UiScenarioFailure("Dashboard repeats the Cameras heading")
    page.get_by_role("button", name="Scan network").click()
    expect(page.get_by_role("heading", name="Scan network")).to_be_visible()
    expect(page.locator("#scan-card")).to_be_visible()
    expect(page.locator("#app-modal")).to_be_hidden()
    expect(page.locator("#scan-start")).to_be_visible()
    expect(page.locator("#scan-run-status")).to_be_hidden()
    expect(page.locator("#scan-results")).to_be_hidden()
    if page.get_by_text("Excluded", exact=True).count():
        raise UiScenarioFailure("Removed subnet is shown as Excluded")
    network_count = page.locator("#scan-network-list .scan-network-row").count()
    expect(page.locator("#scan-network-add")).to_be_hidden()
    detected_cidr = page.evaluate(
        """async () => {
            const response = await fetch('/internal/discovery/networks', {cache: 'no-store'});
            const payload = await response.json();
            return payload.networks?.find(network => network.source === 'detected' && network.selected)?.cidr || null;
        }"""
    )
    if detected_cidr:
        detected_network = page.locator("#scan-network-list .scan-network-row").filter(
            has_text=detected_cidr
        ).first
        expect(detected_network.locator(".scan-network-description")).to_contain_text(
            "Auto-discovered on"
        )
        if detected_network.get_by_role("button", name="Remove").count():
            raise UiScenarioFailure("Detected subnet has a delete action")
        detected_checkbox = detected_network.get_by_role(
            "checkbox", name=f"Include {detected_cidr} in scans"
        )
        detected_checkbox.uncheck()
        expect(page.locator("#scan-network-list .scan-network-row")).to_have_count(
            network_count
        )
        detected_network = page.locator("#scan-network-list .scan-network-row").filter(
            has_text=detected_cidr
        ).first
        detected_checkbox = detected_network.get_by_role(
            "checkbox", name=f"Include {detected_cidr} in scans"
        )
        expect(detected_checkbox).not_to_be_checked()
        expect(detected_checkbox).to_be_enabled()
        page.get_by_role("button", name="Collapse scan network").click()
        page.get_by_role("button", name="Scan network").click()
        detected_network = page.locator("#scan-network-list .scan-network-row").filter(
            has_text=detected_cidr
        ).first
        detected_checkbox = detected_network.get_by_role(
            "checkbox", name=f"Include {detected_cidr} in scans"
        )
        expect(detected_checkbox).not_to_be_checked()
        detected_checkbox.check()
        detected_network = page.locator("#scan-network-list .scan-network-row").filter(
            has_text=detected_cidr
        ).first
        expect(
            detected_network.get_by_role(
                "checkbox", name=f"Include {detected_cidr} in scans"
            )
        ).to_be_checked()
        expect(page.locator("#scan-network-list .scan-network-row")).to_have_count(
            network_count
        )
        expect(page.locator("#scan-network-input")).to_be_enabled()
    custom_networks = page.locator("#scan-network-list .scan-network-row").filter(
        has_text="Custom added"
    )
    if custom_networks.count():
        expect(custom_networks.first.locator(".scan-network-description")).to_have_text(
            "Custom added"
        )
    page.locator("#scan-network-add-toggle").click()
    expect(page.locator("#scan-network-add")).to_be_visible()
    add_button_below_last_subnet = page.locator("#scan-network-add-toggle").evaluate(
        "button => button.getBoundingClientRect().top >= "
        "document.querySelector('#scan-network-list').getBoundingClientRect().bottom"
    )
    if not add_button_below_last_subnet:
        raise UiScenarioFailure("Add subnet action is not below the subnet list")
    page.locator("#scan-network-input").fill("10.0.0.0/8")
    page.locator("#scan-network-add button[type=submit]").click()
    expect(page.locator("#scan-network-settings-status")).to_contain_text(
        "limited to 1,024"
    )
    expect(page.locator("#scan-network-list .scan-network-row")).to_have_count(
        network_count
    )
    expect(page.locator("#scan-log-details")).not_to_have_attribute("open", "")
    expect(page.locator("#scan-log")).to_be_hidden()
    page.locator("#scan-log-details summary").click()
    expect(page.locator("#scan-log")).to_be_visible()
    page.get_by_role("button", name="Collapse scan network").click()

    page.get_by_role("button", name="Add camera").click()
    expect(page.get_by_role("heading", name="Add camera manually")).to_be_visible()
    expect(page.locator("#manual-card")).to_be_visible()
    expect(page.locator("#app-modal")).to_be_hidden()
    page.get_by_role("button", name="Collapse manual camera form").click()

    list_surface = page.locator(".camera-list-surface")
    attached_controls = list_surface.evaluate(
        "surface => surface.contains(document.querySelector('#toolbar')) && "
        "surface.contains(document.querySelector('#camera-table'))"
    )
    if not attached_controls:
        raise UiScenarioFailure("Camera filters and table do not share one surface")

    protocol_badges = set(
        page.locator("#camera-rows .connectivity-protocols .protocol-badge").all_inner_texts()
    )
    missing_protocols = {"ONVIF", "RTSP"} - protocol_badges
    if missing_protocols:
        missing = ", ".join(sorted(missing_protocols))
        raise UiScenarioFailure(f"Connectivity is missing protocol badges: {missing}")

    if page.get_by_role("button", name="Live", exact=True).count():
        raise UiScenarioFailure("Camera actions repeat the thumbnail live-view control")
    page.locator("#camera-rows .preview-cell").first.click()
    expect(page.locator("#live-modal")).to_be_visible()
    expect(page.locator("#live-title")).to_contain_text("Live view")
    page.locator("#live-close").click()

    failures: list[str] = page.locator("#camera-rows tr.camera-row").evaluate_all(
        """
        cards => {
          const failures = [];
          cards.forEach((card, cardIndex) => {
            card.scrollIntoView({block: "center"});
            const actionCell = card.querySelectorAll("td")[4];
            if (!actionCell) {
              failures.push(`camera card ${cardIndex + 1} has no action cell`);
              return;
            }
            const cardBounds = card.getBoundingClientRect();
            const cellBounds = actionCell.getBoundingClientRect();
            const buttons = actionCell.querySelectorAll("button.row-action");
            if (!buttons.length) {
              failures.push(`camera card ${cardIndex + 1} has no action buttons`);
              return;
            }
            buttons.forEach((button, buttonIndex) => {
              const label = button.innerText.trim() || `button ${buttonIndex + 1}`;
              const bounds = button.getBoundingClientRect();
              if (bounds.bottom > cellBounds.bottom + 0.5) {
                failures.push(
                  `${label} extends below its action row on camera card ${cardIndex + 1}`
                );
              }
              if (bounds.bottom > cardBounds.bottom + 0.5) {
                failures.push(`${label} extends below camera card ${cardIndex + 1}`);
              }
            });
          });
          return failures;
        }
        """
    )
    if failures:
        raise UiScenarioFailure("; ".join(failures))

    streams = page.get_by_role("button", name="Streams").first
    expect(streams).to_be_visible()
    streams.click()
    expect(page.locator("#app-modal-title")).to_contain_text("streams")
    modal = page.locator("#app-modal")
    expect(modal).to_be_visible()
    if modal.get_by_text("Frigate destinations").count():
        raise UiScenarioFailure("Streams still duplicates Frigate destination controls")
    page.locator("#app-modal-close").click()


def assert_downstream_password_masking(page: Page) -> None:
    page.add_init_script(
        """
        Object.defineProperty(navigator, "clipboard", {
          configurable: true,
          value: {
            writeText: async value => { window.__camadmiralCopiedText = value; }
          }
        });
        """
    )
    page.reload(wait_until="domcontentloaded")
    page.locator("#camera-rows tr.camera-row").nth(2).wait_for(
        state="attached", timeout=30_000
    )
    streams = page.get_by_role("button", name="Streams").first
    streams.click()
    page.locator("#app-modal-body .downstream-url").filter(
        has_text="rtsp://"
    ).first.wait_for(state="visible", timeout=30_000)
    access = page.evaluate(
        """
        async () => {
          const response = await fetch("/internal/media/access", {
            method: "POST",
            headers: {"X-CamAdmiral-Action": "reveal-media-access"}
          });
          return response.json();
        }
        """
    )
    displayed_urls = page.locator("#app-modal-body .downstream-url")
    displayed = displayed_urls.first.inner_text()
    if ":********@" not in displayed:
        raise UiScenarioFailure("Downstream URL does not show a fixed password mask")
    if access["password"] in page.locator("#app-modal-body").inner_text():
        raise UiScenarioFailure("Streams modal exposes the downstream password")
    attributes = displayed_urls.evaluate_all(
        "elements => elements.flatMap(element => [element.title, element.getAttribute('aria-label')])"
    )
    if any(access["password"] in str(value or "") for value in attributes):
        raise UiScenarioFailure("Downstream password is exposed in a URL attribute")

    localhost = page.get_by_role("radio", name="Localhost")
    with page.expect_response(
        lambda response: response.request.method == "POST"
        and "/stream-address" in response.url
    ) as localhost_save:
        localhost.check()
    if not localhost_save.value.ok:
        raise UiScenarioFailure("Localhost stream address choice was not saved")
    expect(localhost).to_be_checked()
    expect(displayed_urls.first).to_contain_text("@localhost:")

    page.locator("#app-modal-close").click()
    page.reload(wait_until="domcontentloaded")
    page.locator("#camera-rows tr.camera-row").nth(2).wait_for(
        state="attached", timeout=30_000
    )
    page.get_by_role("button", name="Streams").first.click()
    localhost = page.get_by_role("radio", name="Localhost")
    expect(localhost).to_be_checked()
    displayed_urls = page.locator("#app-modal-body .downstream-url")
    expect(displayed_urls.first).to_contain_text("@localhost:")

    lan = page.get_by_role("radio", name="LAN", exact=True)
    with page.expect_response(
        lambda response: response.request.method == "POST"
        and "/stream-address" in response.url
    ) as lan_save:
        lan.check()
    if not lan_save.value.ok:
        raise UiScenarioFailure("LAN stream address choice was not saved")
    expect(lan).to_be_checked()
    expect(displayed_urls.first).not_to_contain_text("@localhost:")

    copy_button = page.locator("#app-modal-body .copy-button").first
    copy_button.click()
    expect(copy_button).to_have_text("✓", timeout=5_000)
    copied = page.evaluate("window.__camadmiralCopiedText")
    parsed = urllib.parse.urlsplit(copied)
    if urllib.parse.unquote(parsed.password or "") != access["password"]:
        raise UiScenarioFailure("Copied downstream URL does not contain its real password")
    if "********" in copied:
        raise UiScenarioFailure("Copied downstream URL contains the display mask")


def assert_camera_unadopt_block_and_restore(page: Page) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    onvif_row = page.locator("#camera-rows tr.camera-row").filter(
        has=page.locator(".protocol-badge", has_text="ONVIF")
    ).first
    expect(onvif_row).to_be_visible(timeout=30_000)
    onvif_row.get_by_role("button", name=re.compile(r"^More actions for")).click()
    action_menu = page.locator("#camera-action-menu")
    expect(action_menu).to_be_visible()
    expect(page.locator("#app-modal")).to_be_hidden()
    action_menu.get_by_role("menuitem", name="Unadopt", exact=True).click()
    expect(page.locator("#confirm-modal")).to_be_visible()
    with page.expect_response(
        lambda response: response.request.method == "DELETE"
        and "/internal/cameras/" in response.url
    ) as unadopt_response:
        page.locator("#confirm-action").click()
    if not unadopt_response.value.ok:
        raise UiScenarioFailure("Camera could not be unadopted through the UI")

    onvif_row = page.locator("#camera-rows tr.camera-row").filter(
        has=page.locator(".protocol-badge", has_text="ONVIF")
    ).first
    expect(onvif_row.get_by_role("button", name="Adopt", exact=True)).to_be_visible(
        timeout=30_000
    )
    expect(onvif_row.locator(".device-status")).to_contain_text("online")
    onvif_row.get_by_role("button", name=re.compile(r"^More actions for")).click()
    action_menu.get_by_role("menuitem", name="Block device").click()
    expect(page.locator("#confirm-modal")).to_be_visible()
    with page.expect_response(
        lambda response: response.request.method == "POST"
        and response.url.endswith("/block")
    ) as block_response:
        page.locator("#confirm-action").click()
    if not block_response.value.ok:
        raise UiScenarioFailure("Device could not be blocked through the UI")

    expect(page.locator("#blocked-count")).to_have_text("1", timeout=30_000)
    page.locator('[data-camera-filter="blocked"]').click()
    blocked_row = page.locator("#camera-rows tr.camera-row").first
    expect(blocked_row).to_be_visible()
    expect(blocked_row).to_contain_text("blocked")
    blocked_row.get_by_role("button", name="Unblock").click()
    with page.expect_response(
        lambda response: response.request.method == "DELETE"
        and "/internal/discovery/blocked/" in response.url
    ) as unblock_response:
        page.locator("#confirm-action").click()
    if not unblock_response.value.ok:
        raise UiScenarioFailure("Device could not be unblocked through the UI")

    expect(page.locator("#blocked-count")).to_have_text("0", timeout=30_000)
    page.locator('[data-camera-filter="all"]').click()
    onvif_row = page.locator("#camera-rows tr.camera-row").filter(
        has=page.locator(".protocol-badge", has_text="ONVIF")
    ).first
    onvif_row.get_by_role("button", name="Adopt", exact=True).click()
    adopt_modal = page.locator("#app-modal")
    expect(adopt_modal.get_by_role("button", name="Adopt camera")).to_be_visible(
        timeout=30_000
    )
    adopt_modal.get_by_role("button", name="Adopt camera").click()
    expect(onvif_row.get_by_role("button", name="Streams")).to_be_visible(timeout=60_000)


def assert_desktop_stream_layout(page: Page) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.locator("#camera-rows tr.camera-row").nth(2).wait_for(
        state="attached", timeout=30_000
    )
    page.get_by_role("button", name="Streams").first.click()
    if page.get_by_text("Frigate destinations").count():
        raise UiScenarioFailure("Streams still duplicates Frigate destination controls")
    page.locator("#app-modal-body .profile-name").first.evaluate(
        "node => { node.textContent = 'MediaProfile_Channel1_MainStream_With_A_Long_Technical_Name'; }"
    )
    collisions = page.locator("#app-modal-body .profile").evaluate_all(
        """
        profiles => profiles.filter(profile => {
          const identity = profile.querySelector('.stream-identity').getBoundingClientRect();
          const metadata = profile.querySelector('.profile-metadata').getBoundingClientRect();
          const access = profile.querySelector('.stream-access').getBoundingClientRect();
          return identity.right > access.left + 0.5 || metadata.right > access.left + 0.5;
        }).length
        """
    )
    if collisions:
        raise UiScenarioFailure("Stream profile details overlap the downstream URL column")


def assert_mobile_settings(page: Page) -> None:
    page.goto(f"{BASE_URL}/settings/notifications", wait_until="domcontentloaded")
    expect(page.locator("#settings-view")).to_be_visible(timeout=15_000)
    expect(page.locator("#dashboard-view")).to_be_hidden()
    expect(page.locator("#incidents-view")).to_be_hidden()
    expect(page.get_by_role("heading", name="Telegram notifications")).to_be_visible()
    expect(page.get_by_role("link", name="Settings")).to_have_attribute("aria-current", "page")
    expect(page.get_by_role("link", name="Notifications")).to_have_attribute("aria-current", "page")

    sync_state: dict[str, object] = {
        "selected": set(),
        "pending_until": {},
        "address_mode": "lan",
        "synced_modes": [],
    }

    def synthetic_frigate_targets(route) -> None:
        selected = sync_state["selected"]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "targets": [
                        {
                            "target_id": "synthetic-target",
                            "name": "Synthetic Frigate",
                            "api_url": "http://192.0.2.20:5000",
                            "connection_status": "connected",
                            "selected_cameras": len(selected),
                            "last_checked_at": "2026-01-01T00:00:00Z",
                            "last_error_code": None,
                            "restart_recommended": False,
                            "address_mode": sync_state["address_mode"],
                        }
                    ]
                }
            ),
        )

    def synthetic_discovery(route) -> None:
        response = route.fetch()
        payload = response.json()
        selected = sync_state["selected"]
        pending_until = sync_state["pending_until"]
        for device in payload.get("devices", []):
            adoption = device.get("adoption")
            if not adoption or not adoption.get("camera_uuid"):
                continue
            camera_uuid = adoption["camera_uuid"]
            status = "not_synced"
            if camera_uuid in selected:
                status = (
                    "pending"
                    if time.monotonic() < pending_until.get(camera_uuid, 0)
                    else "applied"
                )
            adoption["frigate"] = [
                {
                    "target_id": "synthetic-target",
                    "target": "Synthetic Frigate",
                    "selected": camera_uuid in selected,
                    "address_mode": sync_state["address_mode"],
                    "status": status,
                }
            ]
            adoption["thumbnail_captured_at"] = "2026-01-01T00:00:00Z"
        route.fulfill(
            status=response.status,
            content_type="application/json",
            body=json.dumps(payload),
        )

    def synthetic_camera_sync(route) -> None:
        camera_uuid = urllib.parse.unquote(route.request.url.rsplit("/", 1)[-1])
        selected = sync_state["selected"]
        pending_until = sync_state["pending_until"]
        if route.request.method == "POST":
            body = route.request.post_data_json
            sync_state["synced_modes"].append(body.get("address_mode"))
            selected.add(camera_uuid)
            pending_until[camera_uuid] = time.monotonic() + 2.0
            route.fulfill(
                status=202,
                content_type="application/json",
                body='{"status":"syncing","selected":true,"queued":true}',
            )
        else:
            selected.discard(camera_uuid)
            pending_until.pop(camera_uuid, None)
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"status":"removed","selected":false}',
            )

    def synthetic_target_address(route) -> None:
        body = route.request.post_data_json
        sync_state["address_mode"] = body["address_mode"]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "status": "updated",
                    "address_mode": body["address_mode"],
                    "target": {"address_mode": body["address_mode"]},
                }
            ),
        )
    page.route("**/internal/frigate-targets", synthetic_frigate_targets)
    page.route("**/internal/discovery", synthetic_discovery)
    page.route(
        "**/internal/frigate-targets/synthetic-target/address",
        synthetic_target_address,
    )
    page.route(
        "**/internal/frigate-targets/synthetic-target/cameras/**",
        synthetic_camera_sync,
    )
    page.get_by_role("link", name="Integrations").click()
    expect(page).to_have_url(re.compile(r"/settings/integrations$"))
    expect(page.get_by_role("heading", name="Frigate integrations")).to_be_visible()
    expect(page.get_by_role("heading", name="Telegram notifications")).to_be_hidden()
    expect(page.get_by_role("link", name="Integrations")).to_have_attribute("aria-current", "page")
    expect(page.locator("a.app-brand")).to_have_attribute("href", "/")
    target = page.locator(".frigate-target").filter(has_text="Synthetic Frigate")
    expect(target).to_contain_text("0 synced cameras")
    choose = target.get_by_role("button", name="Choose cameras")
    expect(choose).to_be_visible()
    choose.click()
    modal = page.locator("#app-modal")
    expect(page.get_by_role("heading", name="Choose cameras for Synthetic Frigate")).to_be_visible()
    choices = modal.locator(".frigate-camera-choice")
    expect(choices.first).to_be_visible()
    if choices.count() < 1:
        raise UiScenarioFailure("Frigate camera chooser has no adopted cameras")
    if not modal.locator(".frigate-camera-thumbnail img").count():
        raise UiScenarioFailure("Frigate camera chooser has no cached thumbnails")
    first_checkbox = choices.first.get_by_role("checkbox")
    expect(first_checkbox).not_to_be_checked()
    first_checkbox.check()
    expect(modal.get_by_text("Record", exact=True).first).to_be_visible()
    expect(modal.get_by_text("Detect", exact=True).first).to_be_visible()
    expect(modal.get_by_role("radio", name="LAN")).to_be_checked()
    lan_url = modal.locator(".frigate-camera-stream-url").first.inner_text()
    access = page.evaluate(
        """
        async () => {
          const response = await fetch("/internal/media/access", {
            method: "POST",
            headers: {"X-CamAdmiral-Action": "reveal-media-access"}
          });
          return response.json();
        }
        """
    )
    expected_lan_host = access.get("lan_host")
    if (
        not expected_lan_host
        or f"@{expected_lan_host}:" not in lan_url
        or "@localhost:" in lan_url
        or not lan_url.startswith("rtsp://")
    ):
        raise UiScenarioFailure(f"LAN stream preview is invalid: {lan_url}")
    modal.get_by_role("radio", name="Localhost").check()
    expect(modal.locator(".frigate-camera-stream-url").first).to_contain_text("@localhost:")
    with page.expect_response(
        lambda response: response.request.method == "POST"
        and "/internal/frigate-targets/synthetic-target/cameras/" in response.url
    ):
        modal.get_by_role("button", name="Sync cameras").click()
    syncing = modal.get_by_role("button", name="Syncing...")
    expect(syncing).to_be_visible()
    spinner_metrics = syncing.locator(".inline-spinner").evaluate(
        """
        spinner => {
          const bounds = spinner.getBoundingClientRect();
          const style = getComputedStyle(spinner);
          return {width: bounds.width, height: bounds.height, flex: style.flex};
        }
        """
    )
    if (
        abs(float(spinner_metrics["width"]) - 14) > 0.5
        or abs(float(spinner_metrics["height"]) - 14) > 0.5
        or not str(spinner_metrics["flex"]).startswith("0 0")
    ):
        raise UiScenarioFailure(f"Sync spinner geometry is distorted: {spinner_metrics}")
    expect(modal.locator(".frigate-camera-state.applied")).to_have_text(
        "Synced", timeout=10_000
    )
    expect(modal).to_be_visible()
    expect(modal.get_by_role("button", name="Sync cameras")).to_be_enabled()
    if sync_state["address_mode"] != "localhost" or sync_state["synced_modes"] != ["localhost"]:
        raise UiScenarioFailure(f"Frigate target address mode was not persisted: {sync_state}")
    page.locator("#app-modal-close").click()
    expect(target).to_contain_text("1 synced camera")

    target.get_by_role("button", name="Choose cameras").click()
    modal = page.locator("#app-modal")
    first_checkbox = modal.locator(".frigate-camera-choice").first.get_by_role("checkbox")
    expect(first_checkbox).to_be_checked()
    expect(modal.get_by_role("radio", name="Localhost")).to_be_checked()
    expect(modal.locator(".frigate-camera-stream-url").first).to_contain_text("@localhost:")
    first_checkbox.uncheck()
    with page.expect_response(
        lambda response: response.request.method == "DELETE"
        and "/internal/frigate-targets/synthetic-target/cameras/" in response.url
    ):
        modal.get_by_role("button", name="Sync cameras").click()
    expect(modal.locator(".frigate-camera-state.removed").first).to_have_text(
        "Not synced", timeout=5_000
    )
    expect(modal).to_be_visible()
    page.locator("#app-modal-close").click()
    expect(target).to_contain_text("0 synced cameras")

    target.get_by_role("button", name="More actions for Synthetic Frigate").click()
    expect(page.get_by_role("menuitem", name="Test connection")).to_be_visible()
    expect(page.get_by_role("menuitem", name="Repair sync")).to_be_visible()
    expect(page.get_by_role("menuitem", name="Remove integration")).to_be_visible()
    page.keyboard.press("Escape")

    overflow = page.locator("#settings-view .settings-section").evaluate_all(
        "sections => sections.filter(section => section.getBoundingClientRect().right > window.innerWidth + 0.5).length"
    )
    if overflow:
        raise UiScenarioFailure("Settings section extends beyond the mobile viewport")
    add = page.get_by_role("button", name="Add Frigate")
    add.click()
    expect(page.get_by_role("heading", name="Add Frigate")).to_be_visible()
    expect(page.get_by_label("Frigate API URL")).to_have_value(
        "http://127.0.0.1:5000"
    )
    page.locator("#app-modal-close").click()

    page.get_by_role("link", name="Incidents").click()
    expect(page).to_have_url(re.compile(r"/incidents$"))
    expect(page.locator("#incidents-view")).to_be_visible()
    expect(page.get_by_role("heading", name="Incidents")).to_be_visible()
    expect(page.get_by_role("link", name="Incidents")).to_have_attribute("aria-current", "page")


def main() -> int:
    ARTIFACT_DIR.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.webkit.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            http_credentials={"username": "admin", "password": ADMIN_PASSWORD},
        )
        page = context.new_page()
        try:
            assert_mobile_camera_actions(page)
            assert_downstream_password_masking(page)
            assert_camera_unadopt_block_and_restore(page)
            assert_mobile_settings(page)
        except Exception:
            page.screenshot(
                path=str(ARTIFACT_DIR / "mobile-camera-actions.png"),
                full_page=True,
            )
            raise
        finally:
            context.close()
            browser.close()
        desktop_browser = playwright.webkit.launch(headless=True)
        desktop_context = desktop_browser.new_context(
            viewport={"width": 1440, "height": 900},
            http_credentials={"username": "admin", "password": ADMIN_PASSWORD},
        )
        desktop_page = desktop_context.new_page()
        try:
            assert_desktop_stream_layout(desktop_page)
        except Exception:
            desktop_page.screenshot(
                path=str(ARTIFACT_DIR / "desktop-stream-layout.png"),
                full_page=True,
            )
            raise
        finally:
            desktop_context.close()
            desktop_browser.close()
    print("CamAdmiral browser E2E passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
