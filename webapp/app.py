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

import json
import queue
import threading

from flask import Flask, Response, jsonify, request

from integration_layer.api.orchestration_service import MissionExecutionError, OrchestrationService
from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.events import AuditLog
from integration_layer.core.mission import FailureReason, Mission, MissionStep
from integration_layer.core.registry import AdapterRegistry
from integration_layer.core.stats_query import AnthropicStatsQueryEngine, get_query_engine
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
        fault_profile = adapter.get_fault_profile()
        if fault_profile.offline:
            network_condition = "offline"
        elif fault_profile.flaky_failures_remaining > 0 or fault_profile.extra_latency_polls > 0 or fault_profile.random_failure_rates:
            network_condition = "degraded"
        else:
            network_condition = "nominal"
        snapshot.append(
            {
                "robot_id": robot_id,
                "adapter_type": adapter_type,
                "oem_label": OEM_META.get(adapter_type, {}).get("label", adapter_type),
                "oem_color": OEM_META.get(adapter_type, {}).get("color", "#888"),
                "capabilities": [c.capability_type.value for c in adapter.get_capabilities()],
                "power_pct": adapter.get_power_pct(),
                "network_condition": network_condition,
                "failed_sensors": [c.value for c in fault_profile.failed_sensors],
                "extra_latency_polls": fault_profile.extra_latency_polls,
                "random_failure_rates": {k.value: v for k, v in fault_profile.random_failure_rates.items()},
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


def _step_status_dict(s) -> dict:
    return {
        "step_index": s.step_index,
        "status": s.status.value,
        "failure_reason": s.failure_reason.value,
        "detail": s.detail,
        "timestamp_unix_ms": s.timestamp_unix_ms,
    }


def _fleet_stats_dict(fs) -> dict:
    return {
        "total_missions": fs.total_missions,
        "total_succeeded": fs.total_succeeded,
        "total_failed": fs.total_failed,
        "total_paused": fs.total_paused,
        "overall_success_rate": fs.overall_success_rate,
        "failures_by_module": {k.value: v for k, v in fs.failures_by_module.items()},
    }


def _robot_stats_dict(rs) -> dict:
    return {
        "robot_id": rs.robot_id,
        "missions_run": rs.missions_run,
        "missions_succeeded": rs.missions_succeeded,
        "missions_failed": rs.missions_failed,
        "missions_paused": rs.missions_paused,
        "success_rate": rs.success_rate,
        "total_retries": rs.total_retries,
        "retries_by_module": {k.value: v for k, v in rs.retries_by_module.items()},
    }


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
    steps_spec = payload.get("steps")

    _mission_counter += 1
    mission_id = f"gui-mission-{_mission_counter}"

    if steps_spec:
        try:
            steps = [
                MissionStep(capability_type=CapabilityType(s["capability"]), target=s.get("target", "waypoint_1"))
                for s in steps_spec
            ]
        except (ValueError, KeyError) as exc:
            return jsonify({"error": f"invalid steps: {exc}"}), 400
        mission = Mission(mission_id=mission_id, steps=steps)
        primary_capability = steps[0].capability_type
    else:
        try:
            capability = CapabilityType(capability_name)
        except ValueError:
            return jsonify({"error": f"unknown capability {capability_name!r}"}), 400
        mission = Mission(mission_id=mission_id, steps=[MissionStep(capability_type=capability, target="waypoint_1")])
        primary_capability = capability

    try:
        service.assign_mission(mission)
    except MissionExecutionError as exc:
        return jsonify({"error": str(exc)}), 409

    final_status = service.wait_for_completion(mission_id)
    history = service.get_mission_history(mission_id)

    return jsonify(
        {
            "mission_id": mission_id,
            "capability": primary_capability.value,
            "robot_id": mission.assigned_robot_id,
            "adapter_type": type(registry.get(mission.assigned_robot_id)).__name__,
            "status": final_status.status.value,
            "failure_reason": final_status.failure_reason.value,
            "step_count": len(mission.steps),
            "history": [_step_status_dict(s) for s in history],
            "audit": _audit_snapshot(),
        }
    )


# Scoping note, not a defect to fix: registry/audit_log/service are plain
# Python objects with no locks, shared between Flask's request-handling
# threads (now that threaded=True is required for SSE streaming, see
# the bottom of this file) and the background mission-execution thread
# spawned below. Python's GIL makes individual dict/list operations
# atomic enough to avoid corruption at this demo scale, but this is not
# a production-grade concurrency story -- that's a deliberate, known
# scope boundary for this task, not something to add locking around.
_live_mission_queues: dict[str, "queue.Queue"] = {}


@app.post("/api/missions/live")
def api_run_mission_live():
    global _mission_counter
    payload = request.get_json(force=True)
    capability_name = payload.get("capability")
    steps_spec = payload.get("steps")

    _mission_counter += 1
    mission_id = f"gui-mission-{_mission_counter}"

    if steps_spec:
        try:
            steps = [
                MissionStep(capability_type=CapabilityType(s["capability"]), target=s.get("target", "waypoint_1"))
                for s in steps_spec
            ]
        except (ValueError, KeyError) as exc:
            return jsonify({"error": f"invalid steps: {exc}"}), 400
        mission = Mission(mission_id=mission_id, steps=steps)
    else:
        try:
            capability = CapabilityType(capability_name)
        except ValueError:
            return jsonify({"error": f"unknown capability {capability_name!r}"}), 400
        mission = Mission(mission_id=mission_id, steps=[MissionStep(capability_type=capability, target="waypoint_1")])

    event_queue: "queue.Queue" = queue.Queue()
    _live_mission_queues[mission_id] = event_queue

    def _on_progress(status) -> None:
        event_queue.put(_step_status_dict(status))

    def _run() -> None:
        try:
            service.run_mission_with_progress(mission, _on_progress)
        except MissionExecutionError as exc:
            event_queue.put({"error": str(exc)})
        finally:
            event_queue.put(None)  # sentinel: stream is done

    threading.Thread(target=_run, daemon=True).start()

    return jsonify({"mission_id": mission_id})


@app.get("/api/missions/<mission_id>/stream")
def api_mission_stream(mission_id: str):
    event_queue = _live_mission_queues.get(mission_id)
    if event_queue is None:
        return jsonify({"error": f"unknown or already-finished mission_id {mission_id!r}"}), 404

    def _generate():
        while True:
            item = event_queue.get()
            if item is None:
                yield "event: done\ndata: {}\n\n"
                break
            yield f"data: {json.dumps(item)}\n\n"
        _live_mission_queues.pop(mission_id, None)

    return Response(_generate(), mimetype="text/event-stream")


@app.post("/api/robots/<robot_id>/conditions")
def api_set_robot_conditions(robot_id: str):
    adapter = registry.get(robot_id)
    if adapter is None:
        return jsonify({"error": f"unknown robot_id {robot_id!r}"}), 404

    payload = request.get_json(force=True)
    if "offline" in payload:
        adapter.set_offline(bool(payload["offline"]))
    if "flaky_failures" in payload:
        adapter.set_flaky(int(payload["flaky_failures"]))
    if "failed_sensor" in payload:
        try:
            capability = CapabilityType(payload["failed_sensor"])
        except ValueError:
            return jsonify({"error": f"unknown capability {payload['failed_sensor']!r}"}), 400
        adapter.set_sensor_failed(capability, bool(payload.get("failed", True)))
    if "extra_latency_polls" in payload:
        adapter.get_fault_profile().extra_latency_polls = int(payload["extra_latency_polls"])
    if "random_failure_rate" in payload:
        rate_spec = payload["random_failure_rate"]
        try:
            reason = FailureReason(rate_spec["reason"])
        except (KeyError, ValueError) as exc:
            return jsonify({"error": f"invalid random_failure_rate: {exc}"}), 400
        adapter.set_random_failure_rate(reason, float(rate_spec["probability"]))

    return jsonify({"robot_id": robot_id, "fleet": _fleet_snapshot()})


@app.get("/api/robots/<robot_id>/missions")
def api_robot_missions(robot_id: str):
    adapter = registry.get(robot_id)
    if adapter is None:
        return jsonify({"error": f"unknown robot_id {robot_id!r}"}), 404

    mission_ids = audit_log.mission_ids_for_robot(robot_id)
    summaries = []
    for mission_id in mission_ids:
        history = service.get_mission_history(mission_id)
        final = history[-1] if history else None
        summaries.append(
            {
                "mission_id": mission_id,
                "steps_reached": (final.step_index + 1) if final else 0,
                "final_status": final.status.value if final else None,
                "final_failure_reason": final.failure_reason.value if final else None,
            }
        )
    return jsonify(summaries)


@app.get("/api/stats/fleet")
def api_fleet_stats():
    return jsonify(_fleet_stats_dict(service.get_fleet_stats()))


@app.get("/api/stats/robots/<robot_id>")
def api_robot_stats(robot_id: str):
    return jsonify(_robot_stats_dict(service.get_robot_stats(robot_id)))


@app.post("/api/stats/query")
def api_stats_query():
    payload = request.get_json(force=True)
    question = payload.get("question", "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400

    engine = get_query_engine()
    engine_name = "anthropic" if isinstance(engine, AnthropicStatsQueryEngine) else "template"

    fleet_stats = service.get_fleet_stats()
    robot_stats = {robot_id: service.get_robot_stats(robot_id) for robot_id in registry.all_robot_ids()}
    recent_events = audit_log.all_events()[-20:]

    answer = engine.answer(question, fleet_stats, robot_stats, recent_events)
    return jsonify({"answer": answer, "engine": engine_name})


@app.post("/api/reset")
def api_reset():
    global service
    registry.clear()
    audit_log.clear()
    _seed_fleet()
    service = OrchestrationService(registry, audit_log)
    return jsonify({"fleet": _fleet_snapshot()})


INDEX_HTML = (Path(__file__).resolve().parent / "templates" / "index.html").read_text()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False, threaded=True)
