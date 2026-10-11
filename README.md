# IAC Robot Integration Layer — Technical Assessment

The design doc is [`DESIGN.md`](DESIGN.md) — start there. This directory also
contains a **working proof of concept** — two genuinely different OEM
adapters running behind one platform API — in two forms: a CLI demo, and
a real browser-based GUI with a recorded session proving it live.

## Live GUI

`webapp/` is a real Flask app + browser dashboard — not a mockup. Run it
and click through it yourself:

```bash
.venv/bin/pip install flask
.venv/bin/python webapp/app.py
# open http://127.0.0.1:5055
```

It shows the registered fleet (one card per robot, color-coded by OEM),
buttons to run each mission type, and a live audit trail — every click
calls the real `OrchestrationService` used everywhere else in this repo.
`webapp/app.py` is a thin wrapper; it does not reimplement capability
resolution or mission execution.

**Recorded evidence it actually works**, captured by driving the real app
in real headless Chromium via Playwright (`webapp/record_gui_demo.py`) —
not hand-picked screenshots, a scripted click-through of an actually
running server:

- `webapp/output/screenshot_initial.png` — fleet loaded, zero missions run
- `webapp/output/screenshot_final.png` — after all three missions, audit trail populated
- `webapp/output/gui_demo.mp4` — **open this one on macOS.** H.264/yuv420p,
  plays in QuickTime and any browser.
- `webapp/output/gui_demo.webm` — the original Playwright capture (VP8/Opus).
  QuickTime and most native macOS tools won't play `.webm` directly — the
  `.mp4` above is a straight ffmpeg transcode of this file, same content,
  not a re-recording.

To reproduce the recording:

```bash
.venv/bin/pip install playwright
.venv/bin/playwright install chromium
brew install ffmpeg          # optional -- only needed for the .mp4 transcode
.venv/bin/python webapp/record_gui_demo.py
```

![GUI final state](webapp/output/screenshot_final.png)

## Proof of concept (CLI form): what it proves and how to run it

Two robots, two different adapter implementations, same platform API:

| | `spot-01` | `fleetx-01` |
|---|---|---|
| Adapter | `MockRobotAdapter` (`mock_robot/mock_robot_node.py`) | `CloudFleetAdapter` (`mock_robot/cloud_fleet_adapter.py`) |
| Represents | local/ROS2-style OEM | cloud/REST-style OEM |
| Internal mechanics | synchronous-ish, completes a step on first call | async job submission + polling (needs 2 polls to resolve) |
| Power field | `_battery_pct` | `_soc_percent` |
| Error vocabulary | normalized directly | own codes (`ERR_PAYLOAD_FAULT`, etc.) mapped in-adapter |

Both implement the exact same `RobotAdapter` interface
(`integration_layer/core/adapter_base.py`). Nothing OEM-specific —
not the polling behavior, not the field names, not the error codes —
crosses that boundary.

```bash
python3 -m venv .venv
.venv/bin/pip install pytest matplotlib pillow   # pillow only needed for the video
.venv/bin/python -m pytest tests/ -v             # 24/24 — conformance + platform-agnosticism
.venv/bin/python demo/run_demo.py                # runs the demo, writes demo/output/
.venv/bin/python demo/make_pipeline_video.py      # records a video of the pipeline running
```

### CLI pipeline video

`demo/output/video/pipeline_demo.gif` — this one predates the GUI above.
Before `webapp/` existed, there was no GUI and no ffmpeg/screen-capture
tool available, so `demo/make_pipeline_video.py` does the honest
equivalent: it runs the **real** demo code, captures its **real** stdout,
and renders that real output as a scrolling terminal animation, ending on
the real `mission_timeline.png` chart. Nothing in it is staged or
invented — every frame is either genuine captured output or the genuine
generated chart. Kept alongside the GUI recording as a second, independent
form of evidence (and because it doesn't need a browser to view). If
`ffmpeg` is on `PATH` it also writes an `.mp4`; otherwise the GIF (which
plays natively in any browser or image viewer) is the artifact.

`demo/run_demo.py` runs three missions (visual inspection, gas detection,
thermal inspection) through one function, `run_mission()`, that never
branches on which OEM it's talking to. Two missions resolve to `spot-01`,
one to `fleetx-01`, purely because of which robot advertises the
requested capability — not because the calling code knows or cares.
Output:

- `demo/output/demo_run.txt` — captured log of the run (registration,
  per-mission routing decision, full audit trail)
- `demo/output/mission_timeline.png` — visual demonstration: a swimlane
  chart, one lane per robot, showing which OEM served each mission and
  that all three completed

### Where the "switch the robot, don't touch the platform" claim is actually tested

`tests/test_platform_agnosticism.py` is the direct evidence for this.
`run_inspection_mission()` in that file is written once, then:
- run against a registry containing only the local OEM
- run against a registry containing only the cloud OEM
- run against a registry containing both, where routing is decided
  entirely by data (which robot advertises the requested capability),
  never by a branch in the calling code
- run twice against a `Mission` whose `assigned_robot_id` is explicitly
  pinned to each OEM in turn — proving a mission can be redirected to a
  completely different OEM by changing data, not code

### Bugs this process actually caught

Writing the second adapter and the parametrized conformance suite
surfaced two real bugs before they shipped, not hypothetical ones:
1. `MockRobotAdapter.__init__` used `payload or {"visual_camera"}` — an
   explicitly-passed empty `set()` is falsy in Python, so a robot with no
   payload silently got the default payload instead. Fixed to
   `payload if payload is not None else ...`.
2. `CloudFleetAdapter`'s low-power short-circuit in `execute_mission_step`
   returned the correct `LOW_BATTERY_ABORT` status directly, but never
   recorded it — so a subsequent `get_status()` poll (which any real
   caller does) saw "unknown job" and reported `ADAPTER_UNRESPONSIVE`
   instead. Fixed by recording a `forced_status` on every terminal path,
   not just the ones reached through normal job polling.

This is the conformance-suite argument from `DESIGN.md` section 1 playing
out directly: the suite caught both of these because it runs the same
tests against both adapters and insists on the same terminal-status
contract either way.

## What's real vs. illustrative

- **Runs, is tested, and is demoed**: everything under `integration_layer/`
  (except `adapters/ros2_generic/`), `mock_robot/`, `tests/`, `demo/`, and
  `webapp/`. Pure Python, no ROS2/hardware dependency.
- **Illustrative, not built or run**: `integration_layer/adapters/ros2_generic/adapter.py`
  and everything in `cpp_nodes/`. Written against the real `rclpy`/`rclcpp`
  API surface for correctness of shape — implementing the same
  `RobotAdapter` interface the two PoC adapters implement — but there's no
  ROS2 install in the environment this was built in, so they haven't been
  compiled or run against real middleware. `DESIGN.md` section 4 treats
  that verification (bench-testing an adapter against real hardware) as
  real first-30-days engineering work, not something to claim as done here.

## Layout

```
DESIGN.md                            # the design doc
schemas/mission.proto                # the canonical wire contract
integration_layer/
  core/                              # capability model, mission/status schema,
                                     # adapter interface, registry, audit log
  adapters/ros2_generic/adapter.py  # ROS2 worked example (illustrative, see above)
  api/orchestration_service.py      # what orchestration core actually calls
cpp_nodes/                           # latency-critical ROS2 nodes (illustrative)
mock_robot/
  mock_robot_node.py                # OEM adapter #1: local/ROS2-style
  cloud_fleet_adapter.py            # OEM adapter #2: cloud/REST-style
demo/
  run_demo.py                       # PoC demo: same call path, both OEMs
  make_pipeline_video.py            # records demo/run_demo.py's real output as a video
  output/
    demo_run.txt                    # captured log from the last demo run
    mission_timeline.png            # swimlane chart of the last demo run
    video/pipeline_demo.gif         # video of the pipeline running (see "Video" above)
webapp/
  app.py                             # Flask wrapper around the real OrchestrationService
  templates/index.html               # the dashboard (vanilla HTML/CSS/JS, no build step)
  record_gui_demo.py                 # drives the real GUI in headless Chromium, records it
  output/
    screenshot_initial.png           # real screenshot: fleet loaded, no missions run
    screenshot_final.png             # real screenshot: after 3 missions, audit populated
    gui_demo.webm                    # real recorded browser session (Playwright native format)
    gui_demo.mp4                     # same recording, transcoded for macOS playback
tests/
  test_adapter_contract.py          # conformance suite, parametrized over both OEMs
  test_platform_agnosticism.py      # proves the robot can be swapped without
                                     # changing platform-facing calling code
  test_webapp_api.py                # basic tests for the Flask API layer
```

## How I used AI

See the last section of `DESIGN.md` for the original design-doc work.
For this proof-of-concept iteration specifically: I used AI to generate
the second OEM adapter, the parametrized conformance suite, the
platform-agnosticism tests, and the demo script, following the
architecture already committed to in `DESIGN.md`. My review wasn't just
reading the diff — I ran the full suite after each change, and when it
surfaced the two real bugs listed above, I had AI explain the failure,
confirmed the root cause myself against the actual code (not just the
stack trace), and reviewed the fix before accepting it. The first
version of the timeline chart I generated was also wrong in a way I
caught by actually looking at the image — mission bars plotted against
real wall-clock time were overlapping unreadably because in-process mock
calls complete in microseconds — so I had it redone against mission order
instead of wall-clock time, which is what's actually meaningful for a
demo like this.

For the GUI (`webapp/`): AI generated the Flask wrapper, the dashboard
HTML/CSS/JS, and the Playwright recording script. I verified it the same
way as everything else — by running it, not reading it. The first
recording attempt was wrong in a way that mattered: a stale Flask process
from an earlier manual `curl` test was still bound to the port, so the
"initial, zero missions run" screenshot actually showed two missions
already in the audit log. I caught that by looking at the screenshot
content, not just confirming the script exited 0 — a successful script
run and a correct artifact are different claims, and I checked both. I
had the script hardened to fail loudly on a port conflict instead of
silently recording against a stale server, then re-ran it and verified
the corrected initial/final screenshots by hand before treating the
recording as evidence.

Separately: the recording was handed over as a `.webm`, which doesn't
play natively on macOS (QuickTime and most native tools don't support
it). I had the recording script extended to also transcode to H.264
`.mp4` via ffmpeg right after capture, rather than leaving that as a
manual one-off conversion step — then re-ran the whole recording script
end to end (not just the conversion command) to confirm the transcode is
actually wired into the pipeline, and extracted a frame from the
resulting `.mp4` to visually confirm the converted file's content matches
the original recording, not just that `ffprobe` reports a valid codec.
