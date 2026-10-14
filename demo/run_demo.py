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
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from integration_layer.api.orchestration_service import OrchestrationService
from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.customer_registry import Customer, CustomerRegistry
from integration_layer.core.events import AuditLog
from integration_layer.core.mission import (
    FailureReason,
    Mission,
    MissionStatus,
    MissionStep,
    MissionStepStatus,
)
from integration_layer.core.registry import AdapterRegistry
from mock_customer.customer_server import app as customer_server_app
from mock_robot.cloud_fleet_adapter import CloudFleetAdapter
from mock_robot.fault_injection import FaultProfile
from mock_robot.mock_robot_node import MockRobotAdapter

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
CUSTOMER_SERVER_URL_FOR_DEMO: str = ""


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


def build_patrol_mission(mission_id: str) -> Mission:
    """Navigate -> inspect_visual -> navigate -> inspect_thermal: a
    four-step patrol using only existing CapabilityTypes, all on a
    single robot (spot-01 advertises both visual and thermal)."""
    return Mission(
        mission_id=mission_id,
        steps=[
            MissionStep(capability_type=CapabilityType.NAVIGATE, target="waypoint_1"),
            MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="waypoint_1"),
            MissionStep(capability_type=CapabilityType.NAVIGATE, target="waypoint_2"),
            MissionStep(capability_type=CapabilityType.INSPECT_THERMAL, target="waypoint_2"),
        ],
    )


def build_inspection_sweep_mission(mission_id: str) -> Mission:
    """navigate -> inspect_visual: the smallest mission that still
    exercises multi-step advancement."""
    return Mission(
        mission_id=mission_id,
        steps=[
            MissionStep(capability_type=CapabilityType.NAVIGATE, target="waypoint_1"),
            MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="waypoint_1"),
        ],
    )


def _print_step_progress(log) -> callable:
    """Returns a callback suitable for OrchestrationService.run_mission_with_progress()
    that prints each status transition AS IT HAPPENS, not just the final
    result -- this is what makes the CLI demo's output genuinely live for
    multi-step/retry scenarios, unlike the original single-shot
    run_mission() above (which is left untouched)."""

    def _callback(status: MissionStepStatus) -> None:
        detail = f" ({status.detail})" if status.detail else ""
        log(f"    step {status.step_index}: {status.status.value}{detail}")

    return _callback


def run_resilience_demo(service: OrchestrationService, spot: MockRobotAdapter, log) -> None:
    """Runs multi-step missions under four fault conditions against the
    SAME robot (spot-01), demonstrating the module-aware retry policy's
    distinct postures: deterministic comms failures retry and recover,
    sensor failures never retry and pause for operator intervention,
    and probabilistic comms failures are handled by the identical
    retry policy as deterministic ones (the retry layer doesn't care
    how a failure was triggered)."""
    log("\n" + "=" * 72)
    log("Resilience demo -- multi-step missions under four fault conditions")
    log("=" * 72)

    log("\n--- 1. Nominal: patrol mission runs cleanly ---")
    nominal = build_patrol_mission("mission-patrol-nominal")
    status = service.run_mission_with_progress(nominal, _print_step_progress(log))
    log(f"  final: {status.status.value}")

    log("\n--- 2. Deterministic comms fault: spot-01's link fails twice, then recovers ---")
    spot.set_flaky(2)
    flaky = build_patrol_mission("mission-patrol-flaky")
    status = service.run_mission_with_progress(flaky, _print_step_progress(log))
    log(f"  final: {status.status.value} (module=comms, retried and recovered)")

    log("\n--- 3. Sensor fault: spot-01's thermal sensor has failed (no retry, pauses) ---")
    spot.set_flaky(0)
    spot.set_sensor_failed(CapabilityType.INSPECT_THERMAL)
    sensor_down = build_patrol_mission("mission-patrol-sensor-fail")
    status = service.run_mission_with_progress(sensor_down, _print_step_progress(log))
    log(f"  final: {status.status.value} (module=sensor, zero retries, needs operator)")
    spot.set_sensor_failed(CapabilityType.INSPECT_THERMAL, failed=False)

    log("\n--- 4. Probabilistic comms fault: unpredictable-looking failures, same retry policy handles them ---")
    spot.set_fault_profile(FaultProfile(rng_seed=7, random_failure_rates={FailureReason.NETWORK_DEGRADED: 0.5}))
    probabilistic = build_inspection_sweep_mission("mission-sweep-probabilistic")
    status = service.run_mission_with_progress(probabilistic, _print_step_progress(log))
    log(f"  final: {status.status.value} (module=comms, probabilistic failures, same policy as scenario 2)")
    spot.set_fault_profile(FaultProfile())  # reset to healthy


def run_delivery_demo(service: OrchestrationService, spot: MockRobotAdapter, log) -> None:
    """Demonstrates post-mission data capture, upload, and customer
    delivery under two conditions: (1) nominal -- a thermal-inspection
    mission's simulated sensor artifact is captured, uploaded, packaged,
    and pushed to a subscribed customer over a REAL local HTTP call;
    print the actual payload the mock-customer server received. (2) the
    robot goes offline right before the mission completes -- the
    mission itself still COMPLETES, but its data never makes it off the
    robot (DeliveryStatus.UPLOAD_FAILED) -- the "bandwidth-limited site"
    story made concrete."""
    log("\n" + "=" * 72)
    log("Delivery demo -- post-mission data capture, upload, and customer delivery")
    log("=" * 72)

    log("\n--- 1. Nominal: thermal inspection, data delivered to a real subscribed customer ---")
    nominal = Mission(
        mission_id="mission-thermal-for-delivery",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_THERMAL, target="wp1")],
    )
    service.assign_mission(nominal)
    service.wait_for_completion(nominal.mission_id)
    record = service.get_delivery_record(nominal.mission_id)
    log(f"  delivery status: {record.status.value}")
    log(f"  delivered to: {record.delivered_customer_ids}")
    import urllib.request
    import json as json_module
    with urllib.request.urlopen(f"{CUSTOMER_SERVER_URL_FOR_DEMO}/admin/received", timeout=2.0) as response:
        received = json_module.loads(response.read())
    log(f"  mock-customer server actually received: {received[-1] if received else None}")

    log("\n--- 2. Robot goes offline for the whole mission: mission itself fails, delivery is never attempted ---")
    log("  (DeliveryService.deliver() is only reachable through a COMPLETED mission -- the publicly")
    log("   reachable way to show 'data never makes it off the robot' is the mission failing outright;")
    log("   the UPLOAD_FAILED status itself -- a mission that completes but whose data upload separately")
    log("   fails -- is exercised directly in tests/test_data_delivery.py, which can force that exact")
    log("   split via a FaultProfile the public API has no way to toggle mid-mission.)")
    spot.set_offline(True)
    offline_mission = Mission(
        mission_id="mission-thermal-offline",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_THERMAL, target="wp1")],
    )
    service.assign_mission(offline_mission)
    final_status = service.wait_for_completion(offline_mission.mission_id)
    log(f"  mission final status: {final_status.status.value} (module=comms, same retry policy, mission itself fails)")
    log(f"  delivery record: {service.get_delivery_record(offline_mission.mission_id)} (None -- deliver() is never called for a non-COMPLETED mission)")
    spot.set_offline(False)


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

    customer_server = make_server("127.0.0.1", 0, customer_server_app)
    customer_server_thread = threading.Thread(target=customer_server.serve_forever, daemon=True)
    customer_server_thread.start()
    global CUSTOMER_SERVER_URL_FOR_DEMO
    CUSTOMER_SERVER_URL_FOR_DEMO = f"http://127.0.0.1:{customer_server.server_port}"

    customer_registry = CustomerRegistry()
    customer_registry.register(Customer(
        customer_id="acme-industrial",
        webhook_url=f"{CUSTOMER_SERVER_URL_FOR_DEMO}/webhook/deliveries",
        subscribed_capabilities=frozenset({CapabilityType.INSPECT_THERMAL, CapabilityType.INSPECT_VISUAL}),
        site_robot_ids=frozenset({"spot-01"}),
    ))

    log("\nRegistered fleet:")
    for adapter in (spot, fleetx):
        caps = ", ".join(c.capability_type.value for c in adapter.get_capabilities())
        log(f"  {adapter.robot_id:12s} ({type(adapter).__name__:18s}) advertises: {caps}")

    audit_log = AuditLog()
    service = OrchestrationService(
        registry, audit_log, customer_registry=customer_registry, delivery_output_dir=OUTPUT_DIR / "deliveries"
    )

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

    run_resilience_demo(service, spot, log)
    run_delivery_demo(service, spot, log)

    log("\nFleet analytics (updated automatically after every mission -- no manual trigger):")
    fleet_stats = service.get_fleet_stats()
    log(
        f"  total missions: {fleet_stats.total_missions}, succeeded: {fleet_stats.total_succeeded}, "
        f"failed: {fleet_stats.total_failed}, paused: {fleet_stats.total_paused}, "
        f"overall success rate: {fleet_stats.overall_success_rate:.1%}"
    )
    for robot_id in (spot.robot_id, fleetx.robot_id):
        robot_stats = service.get_robot_stats(robot_id)
        log(
            f"  {robot_id}: {robot_stats.missions_run} missions run, "
            f"success rate {robot_stats.success_rate:.1%}, "
            f"{robot_stats.total_retries} total retries"
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "demo_run.txt").write_text("\n".join(log_lines) + "\n")

    customer_server.shutdown()
    customer_server_thread.join(timeout=2)

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
