"""Proof-of-concept demo: two different OEM adapters, one platform API.

Registers one local/ROS2-style robot (MockRobotAdapter) and one
cloud/REST-style robot (CloudFleetAdapter) -- genuinely different
internal mechanics, same RobotAdapter contract. Runs three missions
through OrchestrationService using the exact same calling code
(run_mission() below) regardless of which OEM ends up executing them,
then writes a timeline visualization and a captured log to demo/output/.

Run from the repo root:
    .venv/bin/python demo/run_demo.py

Requires matplotlib for the timeline image (pip install matplotlib);
the mission run itself has no dependency beyond the stdlib and will
still print and succeed without it, just skipping the image.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from integration_layer.api.orchestration_service import OrchestrationService
from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.events import AuditLog
from integration_layer.core.mission import Mission, MissionStatus, MissionStep
from integration_layer.core.registry import AdapterRegistry
from mock_robot.cloud_fleet_adapter import CloudFleetAdapter
from mock_robot.mock_robot_node import MockRobotAdapter

OUTPUT_DIR = Path(__file__).resolve().parent / "output"


@dataclass
class MissionRunRecord:
    mission_id: str
    capability: CapabilityType
    robot_id: str
    start_s: float
    end_s: float
    status: MissionStatus


def run_mission(
    service: OrchestrationService,
    mission_id: str,
    capability: CapabilityType,
    clock_start: float,
) -> MissionRunRecord:
    """The platform-facing call path -- identical for every mission below,
    regardless of which OEM ends up resolving and executing it. This is
    the function that proves the abstraction: it never branches on which
    robot type it's talking to."""
    mission = Mission(
        mission_id=mission_id,
        steps=[MissionStep(capability_type=capability, target="waypoint_1")],
    )

    start = time.monotonic() - clock_start
    service.assign_mission(mission)
    final_status = service.wait_for_completion(mission_id)
    end = time.monotonic() - clock_start

    return MissionRunRecord(
        mission_id=mission_id,
        capability=capability,
        robot_id=mission.assigned_robot_id,
        start_s=start,
        end_s=end,
        status=final_status.status,
    )


def main() -> list[MissionRunRecord]:
    log_lines: list[str] = []

    def log(line: str = "") -> None:
        print(line)
        log_lines.append(line)

    log("=" * 72)
    log("Robot Integration Layer -- proof of concept")
    log("Two OEM adapters, one platform API, run_mission() unchanged across both")
    log("=" * 72)

    registry = AdapterRegistry()
    spot = MockRobotAdapter(
        robot_id="spot-01", payload={"visual_camera", "thermal_camera"}
    )
    fleetx = CloudFleetAdapter(
        robot_id="fleetx-01", fleet_manifest={"sensors": ["voc_sensor"]}
    )
    registry.register(spot)
    registry.register(fleetx)

    log("\nRegistered fleet:")
    for adapter in (spot, fleetx):
        caps = ", ".join(c.capability_type.value for c in adapter.get_capabilities())
        log(f"  {adapter.robot_id:12s} ({type(adapter).__name__:18s}) advertises: {caps}")

    audit_log = AuditLog()
    service = OrchestrationService(registry, audit_log)

    missions = [
        ("mission-visual-inspect", CapabilityType.INSPECT_VISUAL),
        ("mission-gas-detect", CapabilityType.DETECT_GAS),
        ("mission-thermal-inspect", CapabilityType.INSPECT_THERMAL),
    ]

    log("\nRunning missions through the SAME run_mission() call path:\n")
    clock_start = time.monotonic()
    records: list[MissionRunRecord] = []
    for mission_id, capability in missions:
        record = run_mission(service, mission_id, capability, clock_start)
        records.append(record)
        log(
            f"  [{record.mission_id:24s}] capability={record.capability.value:16s} "
            f"-> routed to {record.robot_id:10s} -> {record.status.value}"
        )

    log("\nAudit trail (identical event shape regardless of OEM):")
    for event in audit_log.all_events():
        log(
            f"  t={event.timestamp_unix_ms} mission={event.mission_id or '-':24s} "
            f"robot={event.robot_id:10s} event={event.event_type}"
            + (f" ({event.detail})" if event.detail else "")
        )

    log("\n" + "=" * 72)
    log("Result: 3 missions, 2 different OEM backends, 1 unchanged call path.")
    log("Switching which robot serves a mission (see")
    log("tests/test_platform_agnosticism.py) requires zero changes here.")
    log("=" * 72)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "demo_run.txt").write_text("\n".join(log_lines) + "\n")

    return records


def render_timeline(records: list[MissionRunRecord]) -> None:
    """Swimlane chart: one lane per robot, one slot per mission in the
    order it ran. Deliberately NOT plotted against real wall-clock time --
    these missions run in-process against mock adapters and complete in
    microseconds, so a real-time x-axis collapses everything into an
    unreadable stack at t=0. Mission order is what's actually meaningful
    to show here: which OEM served which request, in what sequence."""
    try:
        import matplotlib

        matplotlib.use("Agg")  # headless, no display needed
        import matplotlib.pyplot as plt
    except ImportError:
        print("\n(matplotlib not installed -- skipping timeline image; `pip install matplotlib` to get it)")
        return

    robot_ids = sorted({r.robot_id for r in records})
    robot_lane = {robot_id: i for i, robot_id in enumerate(robot_ids)}
    status_color = {
        MissionStatus.COMPLETED: "#2e7d32",
        MissionStatus.FAILED: "#c62828",
        MissionStatus.ABORTED: "#f9a825",
    }

    bar_width = 0.8
    fig, ax = plt.subplots(figsize=(3 + 2.2 * len(records), 2.5 + 0.5 * len(robot_ids)))
    for order, record in enumerate(records):
        lane = robot_lane[record.robot_id]
        ax.barh(
            lane,
            bar_width,
            left=order,
            height=0.6,
            color=status_color.get(record.status, "#9e9e9e"),
            edgecolor="black",
        )
        ax.text(
            order + bar_width / 2,
            lane,
            f"{record.capability.value}\n{record.mission_id}\n{record.status.value}",
            ha="center",
            va="center",
            fontsize=8,
            color="white",
        )

    ax.set_yticks(list(robot_lane.values()))
    ax.set_yticklabels(robot_ids)
    ax.set_xlim(-0.2, len(records))
    ax.set_xticks([i + bar_width / 2 for i in range(len(records))])
    ax.set_xticklabels([f"mission {i + 1}" for i in range(len(records))])
    ax.set_xlabel("mission order (same run_mission() call path for every one)")
    ax.set_title("Robot Integration Layer PoC: same platform call, two different OEM adapters")
    fig.tight_layout()

    output_path = OUTPUT_DIR / "mission_timeline.png"
    fig.savefig(output_path, dpi=150)
    print(f"\nTimeline image written to {output_path}")


if __name__ == "__main__":
    records = main()
    render_timeline(records)
