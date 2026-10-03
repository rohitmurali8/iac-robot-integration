"""Web GUI for the Robot Integration Layer PoC.

This is a thin Flask wrapper around the exact same platform code used
everywhere else in this repo (integration_layer, mock_robot) -- it does
not reimplement capability resolution, mission execution, or status
polling. Every "Run mission" click in the browser goes through the real
OrchestrationService.assign_mission() / wait_for_completion() path, the
same calls demo/run_demo.py and the tests use.

Run:
    .venv/bin/python webapp/app.py
Then open http://127.0.0.1:5055
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, request

from integration_layer.api.orchestration_service import MissionExecutionError, OrchestrationService
from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.events import AuditLog
from integration_layer.core.mission import Mission, MissionStep
from integration_layer.core.registry import AdapterRegistry
from mock_robot.cloud_fleet_adapter import CloudFleetAdapter
from mock_robot.mock_robot_node import MockRobotAdapter

app = Flask(__name__)

# One shared fleet + service for the life of the process -- same pattern
# as demo/run_demo.py's main(), just held across requests instead of a
# single script run.
registry = AdapterRegistry()
audit_log = AuditLog()
service = OrchestrationService(registry, audit_log)

OEM_META = {
    "MockRobotAdapter": {"label": "Local / ROS2-style OEM", "color": "#2f6fed"},
    "CloudFleetAdapter": {"label": "Cloud / REST-style OEM", "color": "#7b2fed"},
}

_mission_counter = 0


def _seed_fleet() -> None:
    registry.register(MockRobotAdapter(robot_id="spot-01", payload={"visual_camera", "thermal_camera"}))
    registry.register(CloudFleetAdapter(robot_id="fleetx-01", fleet_manifest={"sensors": ["voc_sensor"]}))


_seed_fleet()


def _fleet_snapshot() -> list[dict]:
    snapshot = []
    for robot_id in registry.all_robot_ids():
        adapter = registry.get(robot_id)
        adapter_type = type(adapter).__name__
        snapshot.append(
            {
                "robot_id": robot_id,
                "adapter_type": adapter_type,
                "oem_label": OEM_META.get(adapter_type, {}).get("label", adapter_type),
                "oem_color": OEM_META.get(adapter_type, {}).get("color", "#888"),
                "capabilities": [c.capability_type.value for c in adapter.get_capabilities()],
            }
        )
    return snapshot


def _audit_snapshot() -> list[dict]:
    return [
        {
            "timestamp_unix_ms": e.timestamp_unix_ms,
            "mission_id": e.mission_id,
            "robot_id": e.robot_id,
            "event_type": e.event_type,
            "detail": e.detail,
        }
        for e in audit_log.all_events()
    ]


@app.get("/")
def index():
    return INDEX_HTML


@app.get("/api/fleet")
def api_fleet():
    return jsonify(_fleet_snapshot())


@app.get("/api/audit")
def api_audit():
    return jsonify(_audit_snapshot())


@app.post("/api/missions")
def api_run_mission():
    global _mission_counter
    payload = request.get_json(force=True)
    capability_name = payload.get("capability")
    try:
        capability = CapabilityType(capability_name)
    except ValueError:
        return jsonify({"error": f"unknown capability {capability_name!r}"}), 400

    _mission_counter += 1
    mission_id = f"gui-mission-{_mission_counter}"
    mission = Mission(
        mission_id=mission_id,
        steps=[MissionStep(capability_type=capability, target="waypoint_1")],
    )

    try:
        service.assign_mission(mission)
    except MissionExecutionError as exc:
        return jsonify({"error": str(exc)}), 409

    final_status = service.wait_for_completion(mission_id)

    return jsonify(
        {
            "mission_id": mission_id,
            "capability": capability.value,
            "robot_id": mission.assigned_robot_id,
            "adapter_type": type(registry.get(mission.assigned_robot_id)).__name__,
            "status": final_status.status.value,
            "failure_reason": final_status.failure_reason.value,
            "audit": _audit_snapshot(),
        }
    )


@app.post("/api/reset")
def api_reset():
    registry.clear()
    audit_log.clear()
    _seed_fleet()
    return jsonify({"fleet": _fleet_snapshot()})


INDEX_HTML = (Path(__file__).resolve().parent / "templates" / "index.html").read_text()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)
