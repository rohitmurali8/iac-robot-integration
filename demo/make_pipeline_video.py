"""Renders an animated recording of the PoC pipeline running.

There is no GUI in this project to screen-record, and no ffmpeg/screen
capture tool available in the environment this was built in -- so this
does the honest equivalent: it runs the real demo (the actual
integration_layer/registry/orchestration_service code, not a script),
captures its real stdout, and renders that real output as a scrolling
terminal animation, ending on the real mission_timeline.png chart. No
line in the video is invented -- every frame is either real captured
output or the real generated chart.

Produces an animated GIF (Pillow can write these without any external
binary). Also writes an MP4 if ffmpeg happens to be on PATH.

Run from the repo root:
    .venv/bin/python demo/make_pipeline_video.py
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUTPUT_DIR = ROOT / "demo" / "output"
VIDEO_DIR = OUTPUT_DIR / "video"
FRAMES_DIR = VIDEO_DIR / "frames"

CANVAS_SIZE = (1280, 800)
BG_COLOR = (18, 18, 18)
FG_COLOR = (120, 230, 120)
DIM_COLOR = (90, 90, 90)
FONT_SIZE = 16
LINE_HEIGHT = 22
MARGIN = 24
LINES_PER_FRAME = 1       # how many new lines reveal per terminal frame
FRAME_MS_PER_LINE = 180   # how long each terminal frame is shown
FINAL_HOLD_FRAMES = 6     # extra copies of the last terminal frame
CHART_HOLD_FRAMES = 14    # frames spent showing the real timeline chart


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in (
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Supplemental/Courier New.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def capture_real_demo_output() -> list[str]:
    """Runs the actual demo code and captures its actual stdout -- this
    is not sample text, it's the real run_mission() calls against the
    real MockRobotAdapter/CloudFleetAdapter executing right now."""
    import demo.run_demo as run_demo

    buf = io.StringIO()
    with redirect_stdout(buf):
        records = run_demo.main()
        run_demo.render_timeline(records)
    return buf.getvalue().splitlines()


def render_terminal_frame(visible_lines: list[str], font: ImageFont.FreeTypeFont) -> Image.Image:
    img = Image.new("RGB", CANVAS_SIZE, BG_COLOR)
    draw = ImageDraw.Draw(img)

    draw.text((MARGIN, 8), "$ .venv/bin/python demo/run_demo.py", font=font, fill=DIM_COLOR)

    max_rows = (CANVAS_SIZE[1] - 60) // LINE_HEIGHT
    start = max(0, len(visible_lines) - max_rows)
    y = 40
    for line in visible_lines[start:]:
        draw.text((MARGIN, y), line, font=font, fill=FG_COLOR)
        y += LINE_HEIGHT

    return img


def build_terminal_frames(lines: list[str], font: ImageFont.FreeTypeFont) -> list[Image.Image]:
    frames = []
    for i in range(LINES_PER_FRAME, len(lines) + LINES_PER_FRAME, LINES_PER_FRAME):
        frames.append(render_terminal_frame(lines[:i], font))
    frames.extend([frames[-1]] * FINAL_HOLD_FRAMES)
    return frames


def build_chart_frames() -> list[Image.Image]:
    chart_path = OUTPUT_DIR / "mission_timeline.png"
    if not chart_path.exists():
        return []
    chart = Image.open(chart_path).convert("RGB")

    canvas = Image.new("RGB", CANVAS_SIZE, (255, 255, 255))
    scale = min(CANVAS_SIZE[0] / chart.width, CANVAS_SIZE[1] / chart.height)
    resized = chart.resize((int(chart.width * scale), int(chart.height * scale)))
    paste_x = (CANVAS_SIZE[0] - resized.width) // 2
    paste_y = (CANVAS_SIZE[1] - resized.height) // 2
    canvas.paste(resized, (paste_x, paste_y))

    return [canvas] * CHART_HOLD_FRAMES


def main() -> None:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)

    print("Running the real demo pipeline to capture real output...")
    lines = capture_real_demo_output()

    font = _load_font(FONT_SIZE)
    terminal_frames = build_terminal_frames(lines, font)
    chart_frames = build_chart_frames()
    all_frames = terminal_frames + chart_frames

    for i, frame in enumerate(all_frames):
        frame.save(FRAMES_DIR / f"frame_{i:04d}.png")

    gif_path = VIDEO_DIR / "pipeline_demo.gif"
    all_frames[0].save(
        gif_path,
        save_all=True,
        append_images=all_frames[1:],
        duration=FRAME_MS_PER_LINE,
        loop=0,
    )
    print(f"Wrote {gif_path} ({len(all_frames)} frames, {len(all_frames) * FRAME_MS_PER_LINE / 1000:.1f}s)")

    if shutil.which("ffmpeg"):
        mp4_path = VIDEO_DIR / "pipeline_demo.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-framerate", str(1000 // FRAME_MS_PER_LINE),
                "-i", str(FRAMES_DIR / "frame_%04d.png"),
                "-pix_fmt", "yuv420p", str(mp4_path),
            ],
            check=True,
            capture_output=True,
        )
        print(f"Wrote {mp4_path}")
    else:
        print("ffmpeg not found on PATH -- GIF is the video artifact (plays in any browser/image viewer).")


if __name__ == "__main__":
    main()
