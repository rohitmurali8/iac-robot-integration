# IAC Robot Integration Layer — Technical Assessment

The design doc is [`DESIGN.md`](DESIGN.md) — start there. This directory also
contains a code scaffold implementing the architecture the doc describes,
against a generic ROS2 robot as the worked example.

## What's real vs. illustrative

- **Runs, and is verified to run**: everything under `integration_layer/`
  (except `adapters/ros2_generic/`), `mock_robot/`, and `tests/`. This is
  pure Python with no ROS2/hardware dependency — the capability model,
  mission schema, adapter interface, registry, orchestration service, and
  the conformance test suite. `tests/test_adapter_contract.py` passes
  against `MockRobotAdapter` (11/11), and the registry→orchestration-service
  path was smoke-tested end to end (capability-based robot resolution
  correctly picks the right robot for a request, and correctly rejects a
  request no registered robot can serve).
- **Illustrative, not built or run**: `integration_layer/adapters/ros2_generic/adapter.py`
  and everything in `cpp_nodes/`. These are written against the real
  `rclpy`/`rclcpp` API surface for correctness of shape — the same
  `RobotAdapter` interface the mock adapter implements — but there's no
  ROS2 install in the environment this was built in, so they haven't been
  compiled or run against real middleware. Section 4 of `DESIGN.md` treats
  that verification (bench-testing an adapter against real hardware) as
  real first-30-days engineering work, not something to claim as done here.

## Layout

```
DESIGN.md                          # the actual assessment answer
schemas/mission.proto              # the canonical wire contract
integration_layer/
  core/                            # capability model, mission/status schema,
                                    # adapter interface, registry, audit log
  adapters/ros2_generic/adapter.py # worked example (illustrative, see above)
  api/orchestration_service.py     # what orchestration core actually calls
cpp_nodes/                         # latency-critical ROS2 nodes (illustrative)
mock_robot/                        # runnable simulated robot, no hardware needed
tests/test_adapter_contract.py     # the conformance suite every adapter must pass
```

## Running the parts that run

```bash
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -v
```

## How I used AI

See the last section of `DESIGN.md`.
