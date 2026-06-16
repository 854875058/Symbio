"""Capture Web UI screenshots for the README using Playwright.

Usage:
    python tools/capture_screenshots.py [base_url] [out_dir]

Requires the dev server running (default http://127.0.0.1:9112) and
`pip install playwright && playwright install chromium`.
Screenshots are written to assets/screenshots/.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9112"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "assets/screenshots")
OUT.mkdir(parents=True, exist_ok=True)

# (nav data-page, filename, full_page, optional scroll selector before shot)
PAGES = [
    ("chat", "ui-chat.png", False, None),
    ("dashboard", "ui-dashboard.png", True, None),
    ("security", "ui-security.png", True, None),
    ("evolution", "ui-flywheel.png", True, None),
    ("computer-use", "ui-computer-use.png", False, None),
    ("ontology", "ui-ontology.png", False, None),
]


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.goto(f"{BASE}/ui", wait_until="networkidle", timeout=30000)
        # force warm light theme
        page.evaluate("localStorage.setItem('symbio-theme','light')")
        page.reload(wait_until="networkidle")
        time.sleep(1.2)

        for data_page, fname, full, scroll_sel in PAGES:
            tab = page.query_selector(f'.nav-tab[data-page="{data_page}"]')
            if tab is None:
                print(f"skip {data_page}: no tab")
                continue
            tab.click()
            time.sleep(1.4)  # let lazy load + fetch settle

            # Computer Use: drive a real session so the audit timeline is populated.
            if data_page == "computer-use":
                try:
                    page.fill("#cu-start-url", f"{BASE}/ui")
                    page.click("#btn-cu-create")
                    time.sleep(1.5)
                    page.fill("#cu-goal", f"open {BASE}/ui and read the page")
                    for _ in range(3):  # navigate -> screenshot -> extract_text
                        page.click("#btn-cu-plan")
                        time.sleep(5.0)  # real navigation can take ~4s; avoid racing the planner
                    time.sleep(4.0)  # let toasts fade before the shot
                    full = True
                except Exception as e:
                    print(f"computer-use drive failed: {e}")

            if scroll_sel:
                page.eval_on_selector(scroll_sel, "el => el.scrollIntoView()")
                time.sleep(0.4)
            path = OUT / fname
            page.screenshot(path=str(path), full_page=full)
            print(f"saved {path}")

        browser.close()


if __name__ == "__main__":
    main()
