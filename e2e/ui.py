from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


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
              const target = document.elementFromPoint(
                bounds.left + bounds.width / 2,
                bounds.bottom - 1
              );
              if (target !== button && !button.contains(target)) {
                failures.push(
                  `${label} bottom edge is clipped on camera card ${cardIndex + 1}`
                );
              }
            });
          });
          return failures;
        }
        """
    )
    if failures:
        raise UiScenarioFailure("; ".join(failures))


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
