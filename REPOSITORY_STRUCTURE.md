# Repository Structure

This is the file-by-file map: what every file does, and which other files actually
use it. `README.md` covers what to run and what it proves; `DESIGN.md` covers the
conceptual architecture; this document is the concrete "where is X and who calls it"
reference, generated against the real file list and real `import` statements rather
than a cached memory of what was built.

Relationships below come from `grep`ing actual `import`/`from ... import` statements
across the repo, not from reading docstrings alone — where a file lists "imported
by," that's a real dependency, not a guess.

## Top-level layout

```
DESIGN.md                      design doc (the spec this PoC implements)
README.md                      how to run everything, what's real vs. illustrative
REPOSITORY_STRUCTURE.md         this file
Dockerfile                      runs webapp/app.py (or tests/demo) in a container -- real Python platform only
docker-compose.yml              one-command `docker compose up --build`
.dockerignore                   keeps .git/.venv/__pycache__ out of the build context
requirements.txt                core deps the Docker image installs (Flask, pytest, matplotlib, pillow)
requirements-optional.txt       playwright (GUI recording) + anthropic (real LLM queries) -- NOT in the image
pyproject.toml                  pytest config (pythonpath=".", testpaths=["tests"])
.gitignore                      ignores .venv/, __pycache__/, demo/output/video/frames/, deliveries/
schemas/mission.proto           canonical wire contract (hand-mirrored in Python, see below)
cpp_nodes/                      illustrative only -- not built or run (not in the Docker image either), see below
integration_layer/              the platform: capability model, mission schema, orchestration, OEM-agnostic core
mock_robot/                     the two real (simulated) OEM robot adapters
mock_customer/                  a real second Flask process simulating a customer's receiving system
webapp/                         the live GUI (Flask + vanilla JS) and its recording script
demo/                           CLI proof-of-concept script + pipeline video renderer
tests/                          the full automated test suite (206 tests)
docs/                           presentation slide deck, superpowers plans/specs/skill-process artifacts
```

## Execution entry points

These are the files you actually run; everything else is a library they import.

| Command | What it does |
|---|---|
| `.venv/bin/python -m pytest tests/ -v` | Full test suite |
| `.venv/bin/python demo/run_demo.py` | CLI proof of concept: 3 missions across both OEMs, 4-posture resilience demo, delivery demo. Writes `demo/output/` |
| `.venv/bin/python demo/make_pipeline_video.py` | Renders `demo/run_demo.py`'s captured stdout as a scrolling terminal animation video/gif |
| `.venv/bin/python webapp/app.py` | Starts the live GUI at `http://127.0.0.1:5055` |
| `.venv/bin/python webapp/record_gui_demo.py` | Drives the real GUI in headless Chromium via Playwright, writes `webapp/output/` (screenshots + video) |
| `.venv/bin/python docs/presentation/export_pdf.py` | Renders `docs/presentation/orchestration_walkthrough.html` to a 12-slide PDF |
| `docker compose up --build` | Builds and runs `webapp/app.py` in a container, published at `http://127.0.0.1:5055` — see README.md's "Run it with Docker" |

## `integration_layer/core/` — the OEM-agnostic platform

Every file here is pure Python, has zero knowledge of any specific OEM, and is
exercised by both OEM adapters identically (that symmetry is what
`tests/test_platform_agnosticism.py` and `tests/test_adapter_contract.py` pin).

| File | Role | Imported by |
|---|---|---|
| `mission.py` | `Mission`, `MissionStep`, `MissionStepStatus`, `MissionStatus`, `FailureReason`, `SystemModule` — the shared data schema every other file in the repo speaks | Nearly everything (24 files) |
| `capability_model.py` | `CapabilityType`, `CapabilityDescriptor` — what a robot can advertise, and the `.satisfies()` check routing uses | Nearly everything (20 files) |
| `adapter_base.py` | `RobotAdapter` ABC — the 5-method interface every OEM adapter implements and the conformance suite enforces | `registry.py`, both OEM adapters, `ros2_generic/adapter.py` |
| `registry.py` | `AdapterRegistry` — capability-based lookup ("who can do X right now"), never a hardcoded robot name | `mission_executor.py`, `orchestration_service.py`, `webapp/app.py`, `demo/run_demo.py` |
| `events.py` | `AuditEvent`, `AuditLog` — indexed by mission/robot at write time (O(k) reads, not full scans) | `mission_executor.py`, `orchestration_service.py`, `webapp/app.py`, `demo/run_demo.py` |
| `retry_policy.py` | `FailureReason -> SystemModule -> RetryStrategy` table; `classify_failure()`, `strategy_for()`, `backoff_for_attempt()` | `step_runner.py`, `mission_executor.py`, `mission_analytics.py`, `data_delivery.py`, `stats_query.py` |
| `step_runner.py` | `StepRunner` — drives exactly one mission step (including its own retries) to a terminal outcome | `mission_executor.py`, `orchestration_service.py` |
| `mission_executor.py` | `MissionExecutor` — cross-step sequencing, per-step adapter re-resolution, the terminal-state hook that fires analytics + artifact capture + delivery exactly once per mission | `orchestration_service.py` only |
| `mission_analytics.py` | `MissionAnalytics`, `RobotStats`, `FleetStats` — incremental fleet/robot stats, updated automatically on every mission's terminal state | `mission_executor.py`, `orchestration_service.py`, `webapp/app.py`, `stats_query.py`, `data_delivery.py` |
| `artifact_capture.py` | `ArtifactCapture` — local, no-network simulated sensor-data capture on sensor-capability step completion | `mission_executor.py`, `data_delivery.py` |
| `customer_registry.py` | `Customer`, `CustomerRegistry` — robot+capability subscription matching for data delivery | `data_delivery.py`, `orchestration_service.py`, `webapp/app.py`, `demo/run_demo.py` |
| `data_delivery.py` | `DeliveryPackager`, `DeliveryService`, `DeliveryStatus`, `DeliveryRecord` — two-phase upload+push, both retried via `retry_policy.py` | `mission_executor.py` only |
| `stats_query.py` | `TemplateStatsQueryEngine`, `AnthropicStatsQueryEngine`, `get_query_engine()` — natural-language fleet querying, with timeout/retry/fallback/caching on the LLM path | `webapp/app.py` only |

## `integration_layer/api/`

| File | Role | Imported by |
|---|---|---|
| `orchestration_service.py` | `OrchestrationService` — the ONE class orchestration core/the GUI/the demo actually talk to. Thin facade composing `MissionExecutor` + `MissionAnalytics` + `CustomerRegistry`/`DeliveryService`. `assign_mission()`/`wait_for_completion()` are pinned, byte-for-byte stable since before any resilience/delivery work began | `demo/run_demo.py`, `webapp/app.py`, `tests/test_platform_agnosticism.py`, `tests/test_multistep_missions.py` |

## `integration_layer/adapters/ros2_generic/` and `cpp_nodes/` — illustrative only

`adapter.py` here implements the same `RobotAdapter` interface as the two real
adapters, written against the real `rclpy` API surface for shape-correctness, but
there is no ROS2 install in this environment — it has never been compiled or run.
`cpp_nodes/` is the same story in C++, structured as a real standard `ament_cmake`
package (not a flat file dump):

```
cpp_nodes/
  package.xml                          # ament_cmake, declares rclcpp/sensor_msgs/
                                        # geometry_msgs/std_msgs/std_srvs/diagnostic_msgs
  CMakeLists.txt                       # 5 executables, ament_lint_auto test deps
  README.md                            # package-local readme: node topology diagram
  src/
    safety_monitor_node.cpp            # independent e-stop watchdog
    command_bridge_node.cpp            # high-rate velocity/joint command relay
    battery_monitor_node.cpp           # power subsystem; calls /estop when critical
    sensor_payload_node.cpp            # inspection payload: visual/thermal/gas capture
    diagnostics_aggregator_node.cpp    # aggregates all of the above into /diagnostics
  launch/bringup.launch.py             # brings up all 5 nodes with shared params
  config/params.yaml                   # per-node tunables (declared ROS2 parameters,
                                        # not hard-coded constants)
```

Neither this directory nor `ros2_generic/adapter.py` is imported by anything that
actually runs in this repo; they exist to prove the interface/package shape
generalizes to a real ROS2 deployment, not to claim that deployment was tested
here. Each node still declares its tunables as proper ROS2 parameters (loaded from
`config/params.yaml`) rather than hard-coded constants, and the five nodes
coordinate purely through topics/services — e.g. `battery_monitor_node` calling
`safety_monitor_node`'s `/estop` service directly, with neither node aware of the
other's internals — the same loose-coupling pattern enforced between the two real
Python OEM adapters.

## `mock_robot/` — the two real (simulated) OEM adapters

| File | Role | Imported by |
|---|---|---|
| `mock_robot_node.py` | `MockRobotAdapter` — local/ROS2-style OEM: payload-driven capability advertisement, synchronous-ish step execution, `battery_pct` | `webapp/app.py`, `demo/run_demo.py`, most test files (8 total) |
| `cloud_fleet_adapter.py` | `CloudFleetAdapter` — cloud/REST-style OEM: session-token auth, async job polling, `soc_percent`, `ERR_*` vendor error codes mapped to the shared `FailureReason` taxonomy | `webapp/app.py`, `demo/run_demo.py`, most test files (7 total) |
| `fault_injection.py` | `FaultProfile`, `healthy_profile()` — shared hybrid deterministic+probabilistic fault simulation, consulted identically by both adapters above | Both adapters, `data_delivery.py`, `demo/run_demo.py`, fault/data-delivery tests (5 total) |

## `mock_customer/` — real local HTTP receiving system

| File | Role | Imported by |
|---|---|---|
| `customer_server.py` | A second, independent Flask app simulating where delivered mission reports land (`/webhook/deliveries`, `/admin/simulate_outage`, `/admin/recover`, `/admin/received`, `/admin/reset`). A real process `DeliveryService` makes genuine local HTTP calls against — not a mock | `demo/run_demo.py`, `webapp/app.py`, `tests/test_data_delivery.py`, `tests/test_customer_server.py` |

## `webapp/`

| File | Role |
|---|---|
| `app.py` | Flask wrapper around the real `OrchestrationService` — every GUI click goes through the exact same platform code as the CLI demo and tests. Seeds the fleet + customers, runs a background mock-customer server, exposes ~20 routes (missions, live SSE, fault controls, mission log, deliveries, stats, LLM query, admin OEM-swap) |
| `templates/index.html` | The dashboard itself: fleet health panel, fault-injection controls, live multi-step mission execution (SSE), audit trail, mission log, deliveries panel, fleet analytics, "ask about the fleet," and the OEM-provider-swap demo control |
| `record_gui_demo.py` | Drives `app.py` in real headless Chromium via Playwright and records the session — not hand-picked screenshots, a scripted click-through of an actually running server |

## `demo/`

| File | Role |
|---|---|
| `run_demo.py` | The CLI proof of concept: registers both OEM adapters, runs 3 missions through the identical `run_mission()` call path, a 4-posture resilience demo (nominal / deterministic-retry / sensor-pause / probabilistic-retry), and a delivery demo (real webhook push + offline-mission-fails story). Also defines `build_patrol_mission()`/`build_inspection_sweep_mission()` and `render_timeline()` |
| `make_pipeline_video.py` | Captures `run_demo.py`'s real stdout and renders it as a scrolling terminal animation (predates the GUI; kept as a second, browser-free form of evidence) |

## `tests/` — what each file actually proves

| File | Covers | Exercises |
|---|---|---|
| `test_adapter_contract.py` | **Pinned.** Conformance suite run identically against both OEM adapters | `adapter_base.py`, both adapters |
| `test_platform_agnosticism.py` | **Pinned.** Proves the calling code above the registry never branches on OEM | `orchestration_service.py`, `registry.py` |
| `test_retry_policy.py` | Exhaustiveness + exact backoff arithmetic | `retry_policy.py` |
| `test_fault_injection.py` | `FaultProfile` in isolation and through both real adapters | `fault_injection.py`, both adapters |
| `test_step_runner.py` | Single-step retry/poll logic against a stub adapter | `step_runner.py` |
| `test_audit_log_indexing.py` | `for_mission`/`for_robot`/`mission_ids_for_robot` O(k) behavior | `events.py` |
| `test_mission_analytics.py` | Incremental stats correctness, isolation, dual-truth bucketing | `mission_analytics.py` |
| `test_mission_executor.py` | Cross-step sequencing, capture/delivery terminal-hook wiring, via stub `StepRunner`/`DeliveryService` plus real adapters for routing | `mission_executor.py` |
| `test_multistep_missions.py` | Integration-level: `OrchestrationService` end to end, including delivery | `orchestration_service.py`, `mission_executor.py`, `data_delivery.py` |
| `test_customer_registry.py` | Subscription matching (capability ∩ site-robot intersection) | `customer_registry.py` |
| `test_artifact_capture.py` | Capture only on sensor-capability `COMPLETED`, per-mission isolation | `artifact_capture.py` |
| `test_customer_server.py` | Real local HTTP against a real `make_server`-hosted instance | `mock_customer/customer_server.py` |
| `test_data_delivery.py` | Zip packaging, upload-phase retry via `FaultProfile`, push-phase retry via stub and via the real customer server | `data_delivery.py` |
| `test_stats_query.py` | Template engine; Anthropic engine's timeout/retry/fallback/caching, all via a fake client, zero real network calls | `stats_query.py` |
| `test_webapp_api.py` | Flask route wiring (deliberately thin — the logic lives below it) | `webapp/app.py` |

## `docs/`

| Path | Role |
|---|---|
| `docs/presentation/orchestration_walkthrough.html` + `.pdf` | 12-slide walkthrough: what the PoC proves, architecture, the 5 orchestration steps, a GUI screenshot, run-it-yourself commands, evidence index |
| `docs/presentation/export_pdf.py` | Renders the HTML deck to PDF via headless Chromium, one slide per page |
| `docs/superpowers/plans/`, `docs/superpowers/specs/` | The design spec and implementation plan for the resilience/delivery work, written and executed via the `superpowers` skill process during development — kept as a record of how this was built, not something you need to read to run the code |

## `schemas/mission.proto`

The canonical wire contract `integration_layer/core/mission.py`/`capability_model.py`
are hand-written Python mirrors of — kept hand-written (not generated) so the
scaffold runs without a `protoc` toolchain, per `DESIGN.md`.

## Generated output (not source, don't hand-edit)

| Path | Produced by |
|---|---|
| `demo/output/demo_run.txt`, `mission_timeline.png`, `video/`, `deliveries/*.zip` | `demo/run_demo.py` (+ `make_pipeline_video.py` for `video/`) |
| `webapp/output/screenshot_*.png`, `gui_demo.webm`/`.mp4` | `webapp/record_gui_demo.py` |
| `deliveries/` (top-level, gitignored) | `webapp/app.py`'s runtime use of `OrchestrationService`'s default delivery output dir |
