# IAC Robot Integration Layer — Technical Assessment

The design doc is [`DESIGN.md`](DESIGN.md) — start there. This directory also
contains a **working proof of concept**: two genuinely different OEM
adapters running behind one platform API, with tests and a visual
demonstration proving the abstraction actually holds.

## Proof of concept: what it proves and how to run it

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

### Video

`demo/output/video/pipeline_demo.gif` — there's no GUI in this project to
screen-record (it's a CLI + a generated chart), and no screen-capture
tool/ffmpeg available in the environment this was built in. So
`demo/make_pipeline_video.py` does the honest equivalent instead of
faking a recording: it runs the **real** demo code, captures its **real**
stdout, and renders that real output as a scrolling terminal animation,
ending on the real `mission_timeline.png` chart. Nothing in the video is
staged or invented — every frame is either genuine captured output or the
genuine generated chart. If `ffmpeg` is on `PATH` it also writes an
`.mp4`; otherwise the GIF (which plays natively in any browser or image
viewer, no video player required) is the artifact.

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
  (except `adapters/ros2_generic/`), `mock_robot/`, `tests/`, and `demo/`.
  Pure Python, no ROS2/hardware dependency.
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
tests/
  test_adapter_contract.py          # conformance suite, parametrized over both OEMs
  test_platform_agnosticism.py      # proves the robot can be swapped without
                                     # changing platform-facing calling code
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
