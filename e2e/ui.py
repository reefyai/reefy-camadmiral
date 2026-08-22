from __future__ import annotations

import os
import re
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
    page.wait_for_function(
        "document.querySelectorAll('#camera-rows tr.camera-row').length >= 3",
        timeout=30_000,
    )

    action_layout = page.locator(".dashboard-controls").evaluate(
        """
        controls => {
          const scan = controls.querySelector("#scan").getBoundingClientRect();
          const add = controls.querySelector("#show-add-address").getBoundingClientRect();
          const status = controls.querySelector(".scan-status").getBoundingClientRect();
          return {
            sameRow: Math.abs(scan.top - add.top) < 1,
            primaryLast: add.left < scan.left,
            touchTargets: scan.height >= 44 && add.height >= 44,
            statusBelow: status.top >= Math.max(scan.bottom, add.bottom) - 0.5,
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
    expect(page.get_by_role("heading", name="Cameras")).to_be_visible()
    expect(page.locator(".scan-details-link")).to_have_text("View details")

    protocol_badges = set(
        page.locator("#camera-rows .connectivity-protocols .protocol-badge").all_inner_texts()
    )
    missing_protocols = {"ONVIF", "RTSP"} - protocol_badges
    if missing_protocols:
        missing = ", ".join(sorted(missing_protocols))
        raise UiScenarioFailure(f"Connectivity is missing protocol badges: {missing}")

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
    page.wait_for_function(
        "document.querySelectorAll('#camera-rows tr.camera-row').length >= 3",
        timeout=30_000,
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

    copy_button = page.locator("#app-modal-body .copy-button").first
    copy_button.click()
    expect(copy_button).to_have_text("✓", timeout=5_000)
    copied = page.evaluate("window.__camadmiralCopiedText")
    parsed = urllib.parse.urlsplit(copied)
    if urllib.parse.unquote(parsed.password or "") != access["password"]:
        raise UiScenarioFailure("Copied downstream URL does not contain its real password")
    if "********" in copied:
        raise UiScenarioFailure("Copied downstream URL contains the display mask")


def assert_mobile_settings(page: Page) -> None:
    page.goto(f"{BASE_URL}/settings/notifications", wait_until="domcontentloaded")
    expect(page.locator("#settings-view")).to_be_visible(timeout=15_000)
    expect(page.locator("#dashboard-view")).to_be_hidden()
    expect(page.locator("#incidents-view")).to_be_hidden()
    expect(page.get_by_role("heading", name="Telegram notifications")).to_be_visible()
    expect(page.get_by_role("link", name="Settings")).to_have_attribute("aria-current", "page")
    expect(page.get_by_role("link", name="Notifications")).to_have_attribute("aria-current", "page")
    page.get_by_role("link", name="Integrations").click()
    expect(page).to_have_url(re.compile(r"/settings/integrations$"))
    expect(page.get_by_role("heading", name="Frigate integrations")).to_be_visible()
    expect(page.get_by_role("heading", name="Telegram notifications")).to_be_hidden()
    expect(page.get_by_role("link", name="Integrations")).to_have_attribute("aria-current", "page")
    expect(page.locator("a.app-brand")).to_have_attribute("href", "/")
    overflow = page.locator("#settings-view .settings-section").evaluate_all(
        "sections => sections.filter(section => section.getBoundingClientRect().right > window.innerWidth + 0.5).length"
    )
    if overflow:
        raise UiScenarioFailure("Settings section extends beyond the mobile viewport")
    add = page.get_by_role("button", name="Add Frigate")
    add.click()
    expect(page.get_by_role("heading", name="Add Frigate")).to_be_visible()
    expect(page.get_by_label("Frigate API URL")).to_be_visible()
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
    print("CamAdmiral mobile browser E2E passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
