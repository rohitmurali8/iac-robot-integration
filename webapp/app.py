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
import os
import queue
import threading

from flask import Flask, Response, jsonify, request, send_file
from werkzeug.serving import make_server

from integration_layer.api.orchestration_service import MissionExecutionError, OrchestrationService
from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.customer_registry import Customer, CustomerRegistry
from integration_layer.core.events import AuditLog
from integration_layer.core.mission import FailureReason, Mission, MissionStep
from integration_layer.core.registry import AdapterRegistry
from integration_layer.core.stats_query import AnthropicStatsQueryEngine, get_query_engine
from mock_customer.customer_server import app as customer_server_app
from mock_robot.cloud_fleet_adapter import CloudFleetAdapter
from mock_robot.mock_robot_node import MockRobotAdapter

app = Flask(__name__)

# One shared fleet + service for the life of the process -- same pattern
# as demo/run_demo.py's main(), just held across requests instead of a
# single script run.
registry = AdapterRegistry()
audit_log = AuditLog()

# A second, real local Flask server simulating where delivered mission
# reports actually land -- started once, in a background thread, for
# the life of this process. OS-assigned port (0) avoids clashing with
# webapp's own port or any other process on this machine.
# threaded=True: this server will field delivery pushes from
# DeliveryService while the main webapp is concurrently serving GUI
# requests (also threaded, see bottom of file) -- single-threaded
# werkzeug would otherwise serialize those deliveries. The resulting
# concurrent access to customer_server.py's module-level _received/
# _outage globals relies on the GIL the same way _live_mission_queues
# below does; no new locking added here either.
_customer_server = make_server("127.0.0.1", 0, customer_server_app, threaded=True)
_customer_server_thread = threading.Thread(target=_customer_server.serve_forever, daemon=True)
_customer_server_thread.start()
CUSTOMER_SERVER_URL = f"http://127.0.0.1:{_customer_server.server_port}"

customer_registry = CustomerRegistry()
service = OrchestrationService(registry, audit_log, customer_registry=customer_registry)

OEM_META = {
    "MockRobotAdapter": {"label": "Local / ROS2-style OEM", "color": "#2f6fed"},
    "CloudFleetAdapter": {"label": "Cloud / REST-style OEM", "color": "#7b2fed"},
}

_mission_counter = 0


def _seed_fleet() -> None:
    registry.register(MockRobotAdapter(robot_id="spot-01", payload={"visual_camera", "thermal_camera"}))
    registry.register(CloudFleetAdapter(robot_id="fleetx-01", fleet_manifest={"sensors": ["voc_sensor"]}))


# FLEET_SOURCE=k8s: instead of two fixed local-process adapters, the
# fleet is whatever robot-worker pods a Kubernetes namespace actually
# has running right now -- discovered, registered, and kept in sync by
# a background thread (k8s/fleet_discovery.py) rather than a one-shot
# seed call. See k8s/README.md for how to run this against a real
# cluster. Local/default runs (this env var unset) are completely
# unaffected -- same two static adapters as always.
_FLEET_SOURCE = os.environ.get("FLEET_SOURCE", "local")

if _FLEET_SOURCE == "k8s":
    from k8s.fleet_discovery import run_forever as _run_k8s_discovery

    _k8s_namespace = os.environ.get("POOL_NAMESPACE", "default")
    _k8s_interval_s = float(os.environ.get("FLEET_DISCOVERY_INTERVAL_S", "5"))
    _k8s_discovery_thread = threading.Thread(
        target=_run_k8s_discovery, args=(registry, _k8s_namespace, _k8s_interval_s), daemon=True
    )
    _k8s_discovery_thread.start()
else:
    _seed_fleet()


def _seed_customers() -> None:
    customer_registry.register(Customer(
        customer_id="acme-industrial",
        webhook_url=f"{CUSTOMER_SERVER_URL}/webhook/deliveries",
        subscribed_capabilities=frozenset({CapabilityType.INSPECT_VISUAL, CapabilityType.INSPECT_THERMAL}),
        site_robot_ids=frozenset({"spot-01"}),
    ))
    customer_registry.register(Customer(
        customer_id="chemsafe-corp",
        webhook_url=f"{CUSTOMER_SERVER_URL}/webhook/deliveries",
        subscribed_capabilities=frozenset({CapabilityType.DETECT_GAS}),
        site_robot_ids=frozenset({"fleetx-01"}),
    ))


_seed_customers()


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


def _delivery_dict(record) -> dict:
    return {
        "mission_id": record.mission_id,
        "status": record.status.value,
        "has_package": record.package_path is not None,
        "delivered_customer_ids": record.delivered_customer_ids,
        "failed_customer_ids": record.failed_customer_ids,
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
        "total_steps_executed": rs.total_steps_executed,
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
        # Explicit read-modify-write, not a bare attribute mutation on
        # the returned object -- a local adapter's get_fault_profile()
        # returns the live object (mutation alone would already stick),
        # but RemoteRobotAdapter's returns a fresh snapshot fetched over
        # HTTP, so the change has to be sent back explicitly via
        # set_fault_profile() to actually take effect against the pod.
        fault_profile = adapter.get_fault_profile()
        fault_profile.extra_latency_polls = int(payload["extra_latency_polls"])
        adapter.set_fault_profile(fault_profile)
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


@app.get("/api/missions/<mission_id>/delivery")
def api_mission_delivery(mission_id: str):
    record = service.get_delivery_record(mission_id)
    if record is None:
        return jsonify({"error": f"no delivery record for mission_id {mission_id!r}"}), 404
    return jsonify(_delivery_dict(record))


@app.get("/api/robots/<robot_id>/deliveries")
def api_robot_deliveries(robot_id: str):
    if registry.get(robot_id) is None:
        return jsonify({"error": f"unknown robot_id {robot_id!r}"}), 404

    mission_ids = audit_log.mission_ids_for_robot(robot_id)
    deliveries = []
    for mission_id in mission_ids:
        record = service.get_delivery_record(mission_id)
        if record is not None:
            deliveries.append(_delivery_dict(record))
    return jsonify(deliveries)


@app.get("/api/deliveries/<mission_id>/download")
def api_download_delivery(mission_id: str):
    record = service.get_delivery_record(mission_id)
    if record is None or record.package_path is None:
        return jsonify({"error": f"no downloadable package for mission_id {mission_id!r}"}), 404
    # package_path is written relative to the process's cwd (see
    # OrchestrationService's _DEFAULT_DELIVERY_OUTPUT_DIR), but Flask's
    # send_file resolves relative paths against app.root_path
    # (webapp/), not cwd -- resolve to an absolute path first so the
    # two don't disagree about where the file landed.
    absolute_package_path = record.package_path.resolve()
    return send_file(absolute_package_path, as_attachment=True, download_name=absolute_package_path.name)


@app.post("/api/stats/query")
def api_stats_query():
    payload = request.get_json(force=True)
    question = payload.get("question", "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400

    engine = get_query_engine()
    is_anthropic = isinstance(engine, AnthropicStatsQueryEngine)

    fleet_stats = service.get_fleet_stats()
    robot_stats = {robot_id: service.get_robot_stats(robot_id) for robot_id in registry.all_robot_ids()}
    recent_events = audit_log.all_events()[-20:]

    answer = engine.answer(question, fleet_stats, robot_stats, recent_events)
    # Report "anthropic_fallback" rather than "anthropic" when the real API
    # call failed after retries and AnthropicStatsQueryEngine silently
    # degraded to its own internal template engine -- the caller still gets
    # a useful answer, but this repo's practice is to never claim a real LLM
    # answered when it didn't.
    if is_anthropic and engine.last_call_used_fallback:
        engine_name = "anthropic_fallback"
    elif is_anthropic:
        engine_name = "anthropic"
    else:
        engine_name = "template"
    return jsonify({"answer": answer, "engine": engine_name})


def _current_visual_provider_robot_id() -> str | None:
    """Whichever registered robot currently advertises INSPECT_VISUAL,
    excluding fleetx-01 (which serves a different capability entirely
    and must never be touched by this swap). None if no robot currently
    advertises it."""
    for robot_id in registry.all_robot_ids():
        if robot_id == "fleetx-01":
            continue
        adapter = registry.get(robot_id)
        if any(c.capability_type == CapabilityType.INSPECT_VISUAL for c in adapter.get_capabilities()):
            return robot_id
    return None


@app.post("/api/admin/swap_visual_provider")
def api_swap_visual_provider():
    """Demo-only endpoint proving the platform-switching story
    test_platform_agnosticism.py already pins at the code level, live
    in the running GUI: unregisters whichever robot currently serves
    INSPECT_VISUAL and registers a DIFFERENT adapter -- a different OEM
    class entirely, not merely a different instance of the same class
    -- in its place. Toggles back and forth (Mock <-> Cloud) so this can
    be clicked repeatedly in a demo/recording. Nothing about
    OrchestrationService, the mission-control buttons, or any other
    route changes because of this call -- the next "Run visual
    inspection mission" click resolves by capability exactly as it
    always does, and simply reaches whichever adapter is now
    registered."""
    current_id = _current_visual_provider_robot_id()
    if current_id is None:
        return jsonify({"error": "no robot currently serves inspect_visual"}), 409

    current_adapter = registry.get(current_id)
    current_type = type(current_adapter).__name__
    registry.unregister(current_id)

    if current_type == "CloudFleetAdapter":
        new_adapter = MockRobotAdapter(robot_id="spot-01", payload={"visual_camera", "thermal_camera"})
    else:
        new_adapter = CloudFleetAdapter(robot_id="spot-01-cloud", fleet_manifest={"sensors": ["rgb_cam"]})
    registry.register(new_adapter)

    return jsonify(
        {
            "swapped_from": {"robot_id": current_id, "adapter_type": current_type},
            "swapped_to": {"robot_id": new_adapter.robot_id, "adapter_type": type(new_adapter).__name__},
            "fleet": _fleet_snapshot(),
        }
    )


@app.post("/api/reset")
def api_reset():
    global service
    registry.clear()
    audit_log.clear()
    # In k8s mode the background discovery thread (already running,
    # polling every FLEET_DISCOVERY_INTERVAL_S) repopulates the registry
    # from whatever pods currently exist on its own -- calling the
    # static local-process seed here would be wrong in that mode.
    if _FLEET_SOURCE != "k8s":
        _seed_fleet()
    customer_registry.clear()
    _seed_customers()
    service = OrchestrationService(registry, audit_log, customer_registry=customer_registry)
    return jsonify({"fleet": _fleet_snapshot()})


INDEX_HTML = (Path(__file__).resolve().parent / "templates" / "index.html").read_text()


if __name__ == "__main__":
    # Defaults to loopback-only for local dev; the Docker image sets
    # WEBAPP_HOST=0.0.0.0 so the container's published port is actually
    # reachable from outside it -- binding 0.0.0.0 by default on a
    # developer's own machine would unnecessarily expose this demo app
    # to the whole LAN.
    host = os.environ.get("WEBAPP_HOST", "127.0.0.1")
    port = int(os.environ.get("WEBAPP_PORT", "5055"))
    app.run(host=host, port=port, debug=False, threaded=True)
