from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import Locator, Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "e2e-artifacts"
BASE_URL = os.environ.get("CAMADMIRAL_E2E_WEB_URL", "http://127.0.0.1:18089")
ADMIN_PASSWORD = os.environ.get(
    "CAMADMIRAL_E2E_ADMIN_PASSWORD",
    "synthetic-e2e-admin-password",
)


class UiScenarioFailure(RuntimeError):
    pass


def box(locator: Locator, description: str) -> dict[str, float]:
    bounds = locator.bounding_box()
    if bounds is None:
        raise UiScenarioFailure(f"{description} has no rendered bounds")
    return bounds


def bottom(bounds: dict[str, float]) -> float:
    return bounds["y"] + bounds["height"]


def assert_mobile_camera_actions(page: Page) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.wait_for_function(
        "document.querySelectorAll('#camera-rows tr.camera-row').length >= 3",
        timeout=30_000,
    )

    cards = page.locator("#camera-rows tr.camera-row")
    for card_index in range(cards.count()):
        card = cards.nth(card_index)
        card.scroll_into_view_if_needed()
        action_cell = card.locator("td").nth(4)
        card_box = box(card, f"camera card {card_index + 1}")
        cell_box = box(action_cell, f"camera action cell {card_index + 1}")
        buttons = action_cell.locator("button.row-action")
        if buttons.count() == 0:
            raise UiScenarioFailure(f"camera card {card_index + 1} has no action buttons")

        for button_index in range(buttons.count()):
            button = buttons.nth(button_index)
            label = button.inner_text().strip() or f"button {button_index + 1}"
            button_box = box(button, f"{label} on camera card {card_index + 1}")
            if bottom(button_box) > bottom(cell_box) + 0.5:
                raise UiScenarioFailure(
                    f"{label} extends below its action row on camera card {card_index + 1}"
                )
            if bottom(button_box) > bottom(card_box) + 0.5:
                raise UiScenarioFailure(
                    f"{label} extends below camera card {card_index + 1}"
                )
            bottom_edge_is_clickable = button.evaluate(
                """
                element => {
                  const bounds = element.getBoundingClientRect();
                  const target = document.elementFromPoint(
                    bounds.left + bounds.width / 2,
                    bounds.bottom - 1
                  );
                  return target === element || element.contains(target);
                }
                """
            )
            if not bottom_edge_is_clickable:
                raise UiScenarioFailure(
                    f"{label} bottom edge is clipped on camera card {card_index + 1}"
                )


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
