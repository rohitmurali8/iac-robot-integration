"""Renders orchestration_walkthrough.html to a PDF via a real headless
Chromium (the same Playwright dependency webapp/record_gui_demo.py
already uses) -- one slide per page, using Chromium's print engine
rather than a separate PDF-generation library.

Run from the repo root:
    .venv/bin/python docs/presentation/export_pdf.py
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
HTML_PATH = HERE / "orchestration_walkthrough.html"
PDF_PATH = HERE / "orchestration_walkthrough.pdf"


def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(HTML_PATH.as_uri())
        page.wait_for_timeout(300)  # let any image (the embedded screenshot) finish loading
        page.pdf(
            path=str(PDF_PATH),
            width="1280px",
            height="720px",
            print_background=True,
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        )
        browser.close()
    print(f"Wrote {PDF_PATH}")


if __name__ == "__main__":
    main()
