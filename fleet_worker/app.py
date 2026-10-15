"""Runs inside a single Kubernetes pod: wraps exactly ONE robot adapter
instance and exposes the RobotAdapter interface over HTTP, so
integration_layer.core.remote_adapter.RemoteRobotAdapter can drive it
from outside the pod. This uses the SAME Docker image as webapp/app.py
-- only the container's command differs (see k8s/*.yaml) -- so "the
docker image deployed on those robots" is literally the same image,
not a separate build.

Configured entirely by environment variables, the way a real per-robot
deployment would inject identity/config via the Kubernetes Downward
API and a ConfigMap/Deployment spec rather than hard-coded Python:

  ROBOT_TYPE     mockrobot | cloudfleet          (required)
  ROBOT_ID       defaults to $HOSTNAME (the pod name, under Kubernetes)
  MOCK_PAYLOAD   comma-separated, mockrobot only  (default: visual_camera,thermal_camera)
  CLOUD_SENSORS  comma-separated, cloudfleet only (default: rgb_cam)
  WORKER_PORT    default 6000

Run:
    ROBOT_TYPE=mockrobot .venv/bin/python fleet_worker/app.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, request

from integration_layer.core.wire import (
    capability_to_dict,
    fault_profile_from_dict,
    fault_profile_to_dict,
    status_to_dict,
    step_from_dict,
)
from mock_robot.cloud_fleet_adapter import CloudFleetAdapter
from mock_robot.mock_robot_node import MockRobotAdapter

app = Flask(__name__)

ROBOT_TYPE = os.environ.get("ROBOT_TYPE", "mockrobot")
ROBOT_ID = os.environ.get("ROBOT_ID") or os.environ.get("HOSTNAME", "worker-unknown")

if ROBOT_TYPE == "mockrobot":
    _payload = set(os.environ.get("MOCK_PAYLOAD", "visual_camera,thermal_camera").split(","))
    adapter = MockRobotAdapter(robot_id=ROBOT_ID, payload=_payload)
elif ROBOT_TYPE == "cloudfleet":
    _sensors = os.environ.get("CLOUD_SENSORS", "rgb_cam").split(",")
    adapter = CloudFleetAdapter(robot_id=ROBOT_ID, fleet_manifest={"sensors": _sensors})
else:
    raise ValueError(f"unknown ROBOT_TYPE {ROBOT_TYPE!r} -- expected 'mockrobot' or 'cloudfleet'")


@app.get("/health")
def health():
    # Kubernetes readiness/liveness probe target -- also doubles as a
    # quick way to confirm which adapter type/identity a given pod IP
    # is actually running, from outside the pod.
    return jsonify({"status": "ok", "robot_id": ROBOT_ID, "robot_type": ROBOT_TYPE})


@app.get("/capabilities")
def capabilities():
    return jsonify([capability_to_dict(c) for c in adapter.get_capabilities()])


@app.post("/execute_step")
def execute_step():
    payload = request.get_json(force=True)
    step = step_from_dict(payload["step"])
    status = adapter.execute_mission_step(payload["mission_id"], payload["step_index"], step)
    return jsonify(status_to_dict(status))


@app.get("/status")
def get_status():
    mission_id = request.args["mission_id"]
    step_index = int(request.args["step_index"])
    status = adapter.get_status(mission_id, step_index)
    return jsonify(status_to_dict(status))


@app.post("/handle_exception")
def handle_exception():
    payload = request.get_json(force=True)
    adapter.handle_exception(payload["mission_id"], payload["step_index"], payload["reason"])
    return jsonify({"ok": True})


@app.post("/estop")
def estop():
    adapter.estop()
    return jsonify({"ok": True})


# The two routes below proxy concrete-adapter convenience methods that
# aren't part of the RobotAdapter ABC (both MockRobotAdapter and
# CloudFleetAdapter expose them the same way, called directly by
# webapp/app.py's fleet snapshot and fault-injection controls). Needed
# so RemoteRobotAdapter can offer the same surface to those same
# callers when the adapter it's fronting lives in this pod, not
# in-process.


@app.get("/power")
def get_power():
    return jsonify({"power_pct": adapter.get_power_pct()})


@app.get("/fault_profile")
def get_fault_profile_route():
    return jsonify(fault_profile_to_dict(adapter.get_fault_profile()))


@app.post("/fault_profile")
def set_fault_profile_route():
    payload = request.get_json(force=True)
    adapter.set_fault_profile(fault_profile_from_dict(payload))
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("WORKER_PORT", "6000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
