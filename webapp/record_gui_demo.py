"""Drives the real GUI in a real headless browser and records it.

Starts webapp/app.py as a subprocess, opens it in headless Chromium via
Playwright, clicks through the same three missions demo/run_demo.py runs
(visual inspection, gas detection, thermal inspection), and saves:
  - webapp/output/screenshot_initial.png  (fleet loaded, no missions run)
  - webapp/output/screenshot_final.png    (after all three missions)
  - webapp/output/gui_demo.webm           (full session recording,
                                           Playwright's native browser
                                           video capture -- a real
                                           recording of a real GUI, not a
                                           rendered animation)

Run from the repo root:
    .venv/bin/python webapp/record_gui_demo.py
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "webapp" / "output"
PORT = 5055
BASE_URL = f"http://127.0.0.1:{PORT}"

MISSIONS = ["inspect_visual", "detect_gas", "inspect_thermal"]


def assert_port_free(port: int) -> None:
    """Fail loudly instead of silently recording against a stale server.

    A prior run of this script (or app.py directly) left a process bound
    to this port once already, and wait_for_server() happily "succeeded"
    against that stale process -- producing a video whose "initial" state
    wasn't actually initial. This check is what prevents that from
    silently happening again."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(
                f"port {port} is already in use -- a previous server is still running. "
                f"Find it with `lsof -ti:{port}` and kill it before re-running this script."
            )


def wait_for_server(proc: subprocess.Popen, timeout_s: float = 10.0) -> None:
    import urllib.request

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Flask server process exited before becoming ready")
        try:
            urllib.request.urlopen(BASE_URL, timeout=0.5)
            return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError("Flask server did not become ready in time")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    assert_port_free(PORT)

    server = subprocess.Popen(
        [sys.executable, str(ROOT / "webapp" / "app.py")],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        print("Waiting for Flask dev server...")
        wait_for_server(server)
        print("Server ready, launching headless Chromium...")

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                record_video_dir=str(OUTPUT_DIR),
                record_video_size={"width": 1280, "height": 900},
            )
            page = context.new_page()
            page.goto(BASE_URL)
            page.wait_for_selector(".robot-card")

            page.screenshot(path=str(OUTPUT_DIR / "screenshot_initial.png"))
            print("Captured initial screenshot (fleet loaded, no missions run).")

            for capability in MISSIONS:
                page.click(f'button[data-capability="{capability}"]')
                page.wait_for_function(
                    "document.getElementById('last-result').textContent.includes('completed')"
                    " || document.getElementById('last-result').textContent.includes('failed')"
                )
                # Deliberate pause so the recording actually shows each
                # result on screen rather than clicking through instantly.
                page.wait_for_timeout(1200)
                print(f"  ran {capability} mission")

            page.screenshot(path=str(OUTPUT_DIR / "screenshot_final.png"))
            print("Captured final screenshot (all three missions run, audit trail populated).")

            page.wait_for_timeout(500)
            video_path = page.video.path()
            context.close()
            browser.close()

            final_video = OUTPUT_DIR / "gui_demo.webm"
            Path(video_path).rename(final_video)
            print(f"Video saved to {final_video}")

    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    main()
