# iac_cpp_nodes

Latency-critical and subsystem-monitoring ROS2 nodes for the IAC Robot
Integration Layer. Standard `ament_cmake` package layout:

```
cpp_nodes/
  package.xml
  CMakeLists.txt
  src/
    safety_monitor_node.cpp          # independent e-stop watchdog
    command_bridge_node.cpp          # high-rate velocity/joint command relay
    battery_monitor_node.cpp         # power subsystem, calls /estop when critical
    sensor_payload_node.cpp          # inspection payload: visual/thermal/gas capture
    diagnostics_aggregator_node.cpp  # aggregates the above into /diagnostics
  launch/
    bringup.launch.py                # brings up all five nodes with shared params
  config/
    params.yaml                      # per-node tunables, loaded by the launch file
```

**Really built and run — in a dedicated Docker container, not bare-metal in
this repo** (no ROS2 toolchain on the host this was developed on). From the
repo root:

```bash
docker compose --profile ros2 up --build cpp_nodes
# or directly:
docker build -t iac-cpp-nodes -f Dockerfile .   # from inside cpp_nodes/
docker run --rm iac-cpp-nodes
```

This is a real `colcon build` against real `rclcpp`/`sensor_msgs`/
`geometry_msgs`/`std_msgs`/`std_srvs`/`diagnostic_msgs` (`FROM
ros:humble-ros-base`), not a shape-correctness-only claim: all 5 nodes build
and run, `ros2 topic list` shows every expected topic, `/diagnostics`
genuinely aggregates live `/battery_state` + `/safety/estop_active` data, and
`/payload/capture_visual`/`/payload/capture_gas` respond correctly per the
default payload config. Every node is still written with simulated sensor
inputs/hardware reads (see each file's header comment for what's simulated
vs. what a real deployment would replace) — there's no actual LIDAR, BMS, or
camera behind any of this — but the ROS2 graph itself (nodes, topics,
services, parameters, launch) is real, compiled, and exercised, not merely
plausible-looking source.

## Node responsibilities and topology

```
                         /adapter/velocity_target
  Python adapter  ─────────────────────────────────►  command_bridge_node ───► /cmd_vel
       │                                                                        (50Hz)
       │  /estop (service)                /scan
       ▼                                     │
  safety_monitor_node  ◄──────────────────────┘
       │
       │ /safety/estop_active
       ▼
  diagnostics_aggregator_node ───► /diagnostics
       ▲
       │ /battery_state
       │
  battery_monitor_node ──── calls /estop when critical ────► safety_monitor_node

  sensor_payload_node:  /payload/capture_visual | _thermal | _gas  (services)
                         /payload/last_capture                      (topic)
```

Each node owns exactly one subsystem and talks to the others only through
published topics or service calls — no node reaches into another's internals,
the same loose-coupling principle `integration_layer/core/adapter_base.py`
enforces between OEM adapters at the Python layer. `battery_monitor_node`
independently calling `safety_monitor_node`'s `/estop` service when charge
goes critical is the concrete cross-subsystem example: two nodes coordinating
through a published interface, neither aware of the other's implementation.

## Why C++ at all, given the rest of this repo is Python

See `DESIGN.md` section 1. Only `safety_monitor_node` and `command_bridge_node`
are latency-critical in the sense that matters (watchdog timing and control-loop
rate Python's GC/interpreter overhead can't reliably guarantee); the other three
are here in C++ for topology-consistency with a real ROS2 graph, not because
Python couldn't do the job.
