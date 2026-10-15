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
  - webapp/output/gui_demo.mp4            (the same recording transcoded
                                           to H.264/yuv420p via ffmpeg, if
                                           ffmpeg is on PATH -- QuickTime
                                           and most macOS tools don't play
                                           .webm, so this is the one to
                                           actually open on a Mac)

Run from the repo root:
    .venv/bin/python webapp/record_gui_demo.py
"""

from __future__ import annotations

import shutil
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

# Steps for the two live, multi-step missions the GUI exposes -- these
# exercise the orchestration machinery (retry, cross-step sequencing,
# post-mission delivery) that the three single-capability buttons above
# don't: each of those is a single step with no retry/delivery story.
LIVE_PATROL_STEPS_BUTTON = "#run-patrol-live-btn"
LIVE_SWEEP_STEPS_BUTTON = "#run-sweep-live-btn"


def transcode_to_mp4(webm_path: Path) -> Path | None:
    """Converts the Playwright-recorded .webm (VP8/Opus) to an H.264/
    yuv420p .mp4 -- QuickTime and most native macOS video tools can't play
    .webm at all, so without this step the "recording" isn't actually
    viewable on the platform this was built on. Returns None (and leaves
    only the .webm) if ffmpeg isn't available, rather than failing the
    whole recording run over a missing optional tool."""
    if not shutil.which("ffmpeg"):
        print("ffmpeg not found on PATH -- skipping mp4 transcode, .webm is the only output.")
        return None

    mp4_path = webm_path.with_suffix(".mp4")
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(webm_path),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(mp4_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ffmpeg transcode failed (exit {result.returncode}):\n{result.stderr[-2000:]}")
        return None
    return mp4_path


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
            # Landscape, standard video-player aspect ratio -- a tall
            # portrait viewport makes the recording nearly unreadable
            # once played back at normal size. The page itself is much
            # taller than this viewport; the script scrolls each new
            # section into view as it's needed (see scrollIntoView calls
            # below) instead of trying to show the whole page at once.
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                record_video_dir=str(OUTPUT_DIR),
                record_video_size={"width": 1280, "height": 800},
            )
            page = context.new_page()
            page.goto(BASE_URL)
            page.wait_for_selector(".robot-card")

            page.screenshot(path=str(OUTPUT_DIR / "screenshot_initial.png"))
            print("Captured initial screenshot (fleet loaded, no missions run).")
            page.wait_for_timeout(4000)  # let the viewer actually see the empty starting state

            for capability in MISSIONS:
                page.click(f'button[data-capability="{capability}"]')
                page.wait_for_function(
                    "document.getElementById('last-result').textContent.includes('completed')"
                    " || document.getElementById('last-result').textContent.includes('failed')"
                )
                # Deliberate, generous pause so the recording actually
                # shows each result on screen rather than clicking
                # through instantly -- this script's whole job is to
                # produce a watchable walkthrough, not just exercise
                # the API as fast as possible.
                page.wait_for_timeout(3500)
                print(f"  ran {capability} mission")

            # Prove the central platform-agnosticism claim live: the SAME
            # "Run visual inspection mission" button, with ZERO code
            # change, gets served by a completely different OEM adapter
            # once the registered provider is swapped. This is the exact
            # thing tests/test_platform_agnosticism.py pins at the code
            # level -- here it's the same guarantee, visible on screen.
            page.evaluate("document.getElementById('swap-provider-btn').scrollIntoView({block: 'center'})")
            page.wait_for_timeout(1000)
            page.click('button[data-capability="inspect_visual"]')
            page.wait_for_function(
                "document.getElementById('last-result').textContent.includes('completed')"
            )
            page.wait_for_timeout(2500)
            print("  ran visual-inspection mission on the ORIGINAL provider (spot-01, MockRobotAdapter)")

            page.click("#swap-provider-btn")
            page.wait_for_function(
                "document.getElementById('swap-result').textContent.includes('now served by')"
            )
            page.wait_for_timeout(3000)
            print("  swapped the visual-inspection provider to a different OEM adapter class")

            page.click('button[data-capability="inspect_visual"]')
            page.wait_for_function(
                "document.getElementById('last-result').textContent.includes('completed')"
            )
            page.wait_for_timeout(3500)
            print("  ran the SAME button again -- now routed to the NEW provider (spot-01-cloud, CloudFleetAdapter)")

            # Swap back so the rest of the recording (and the committed
            # screenshots) reflect the default fleet, not the swapped one.
            page.click("#swap-provider-btn")
            page.wait_for_function(
                "document.getElementById('swap-result').textContent.includes('now served by')"
            )
            page.wait_for_timeout(1500)
            print("  swapped the provider back to the original adapter")

            # The three single-capability buttons above each run exactly
            # one step with no retry/delivery story. The rest of this
            # script exercises the actual orchestration machinery this
            # whole project is about: live multi-step execution, retry
            # under an injected fault, mission log / delivery visibility,
            # and the natural-language stats query -- so the recording
            # shows the full pipeline, not just three button clicks.

            def run_live_mission_and_wait(button_selector: str, label: str, hold_s: int) -> None:
                page.click(button_selector)
                page.wait_for_function(
                    "document.querySelectorAll('#live-timeline .step-row').length > 0"
                )
                # Keep the timeline (not just the button) in frame for the
                # hold -- in the landscape viewport the button and its
                # results don't both fit on screen at once.
                page.evaluate("document.getElementById('live-timeline').scrollIntoView({block: 'center'})")
                page.wait_for_timeout(2000)  # let the SSE stream fully settle
                page.wait_for_timeout(hold_s * 1000)  # deliberate hold so the timeline is readable
                print(f"  ran live mission: {label}")

            page.evaluate("document.getElementById('run-patrol-live-btn').scrollIntoView({block: 'center'})")
            page.wait_for_timeout(1000)
            run_live_mission_and_wait(LIVE_PATROL_STEPS_BUTTON, "patrol (nominal, multi-step)", hold_s=6)

            # Inject a deterministic comms fault on spot-01, then re-run a
            # live mission -- the timeline will show a retry-attempt row
            # before the step resolves, demonstrating the module-aware
            # retry policy live in the GUI, not just in a log file. This
            # is the single most important moment in the recording, so
            # it gets the longest hold.
            page.evaluate("document.querySelector('[data-flaky-input=\"spot-01\"]').scrollIntoView({block: 'center'})")
            page.wait_for_timeout(800)
            page.fill('[data-flaky-input="spot-01"]', "2")
            page.click('button[data-flaky-btn="spot-01"]')
            page.wait_for_timeout(1000)
            run_live_mission_and_wait(LIVE_SWEEP_STEPS_BUTTON, "sweep (comms fault, retries and recovers)", hold_s=9)
            # Reset the fault so the fleet health panel ends the recording
            # showing a clean, nominal state.
            page.fill('[data-flaky-input="spot-01"]', "0")
            page.click('button[data-flaky-btn="spot-01"]')
            page.wait_for_timeout(1000)

            # Take spot-01 offline and back online -- shows the network
            # health pill react live, and that a mission attempted while
            # offline fails deterministically (connectivity_lost) rather
            # than hanging.
            page.evaluate("document.querySelector('[data-toggle-offline=\"spot-01\"]').scrollIntoView({block: 'center'})")
            page.wait_for_timeout(500)
            page.click('[data-toggle-offline="spot-01"]')
            page.wait_for_timeout(3500)
            print("  took spot-01 offline (network pill updates live)")
            page.click('[data-toggle-offline="spot-01"]')
            page.wait_for_timeout(3500)
            print("  brought spot-01 back online")

            # Mission log + Deliveries panels: populated by clicking
            # "View mission log" on a robot card.
            page.evaluate("document.querySelector('[data-mission-log-btn=\"spot-01\"]').scrollIntoView({block: 'center'})")
            page.wait_for_timeout(800)
            page.click('button[data-mission-log-btn="spot-01"]')
            page.wait_for_function(
                "document.getElementById('mission-log-body').textContent.length > 0"
            )
            page.evaluate("document.getElementById('mission-log-body').scrollIntoView({block: 'start'})")
            page.wait_for_timeout(2500)
            page.evaluate("document.getElementById('deliveries-body').scrollIntoView({block: 'start'})")
            page.wait_for_timeout(3500)
            print("  loaded spot-01's mission log + deliveries")

            # Ask-about-the-fleet: a real natural-language query against
            # the live fleet/robot stats, answered by whichever engine is
            # configured (template by default; Anthropic if
            # ANTHROPIC_API_KEY is set in this environment).
            page.evaluate("document.getElementById('ask-input').scrollIntoView({block: 'center'})")
            page.wait_for_timeout(800)
            page.fill("#ask-input", "Which robot is the most reliable, and what's the fleet's overall success rate?")
            page.click("#ask-btn")
            page.wait_for_function(
                "const t = document.getElementById('ask-answer').textContent;"
                " t.length > 0 && !t.includes('Ask a question') && t !== 'Thinking...'"
            )
            page.wait_for_timeout(6500)
            print("  asked a natural-language question about the fleet")

            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(800)
            page.screenshot(path=str(OUTPUT_DIR / "screenshot_final.png"), full_page=True)
            print("Captured final screenshot (full page: missions, live retries, mission log, deliveries, analytics, ask-the-fleet).")

            # Slow scroll through the whole page so the recording ends
            # with a readable pass over every section, not a hard cut.
            page_height = page.evaluate("document.body.scrollHeight")
            scroll_steps = max(1, page_height // 500)
            for i in range(scroll_steps + 1):
                page.evaluate(f"window.scrollTo(0, {i * 500})")
                page.wait_for_timeout(1200)
            page.wait_for_timeout(3000)

            page.wait_for_timeout(500)
            video_path = page.video.path()
            context.close()
            browser.close()

            final_video = OUTPUT_DIR / "gui_demo.webm"
            Path(video_path).rename(final_video)
            print(f"Video saved to {final_video}")

            mp4_path = transcode_to_mp4(final_video)
            if mp4_path is not None:
                print(f"Playable mp4 saved to {mp4_path}")

    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    main()
