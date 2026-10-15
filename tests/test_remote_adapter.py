"""Tests for RemoteRobotAdapter against a REAL fleet_worker/app.py
process over a REAL socket -- not test_client(), not a mocked HTTP
layer. fleet_worker/app.py is launched as an actual subprocess (the
same way it would run as a container's entrypoint in a Kubernetes pod),
matching this repo's established practice of hitting real dependencies
rather than mocking the transport layer (see test_customer_server.py /
test_data_delivery.py for the same pattern against mock_customer's
server).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.mission import FailureReason, MissionStatus, MissionStep
from integration_layer.core.remote_adapter import RemoteRobotAdapter
from mock_robot.fault_injection import FaultProfile

ROOT = Path(__file__).resolve().parent.parent


def _wait_for_health(port: int, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5)
            return
        except Exception:
            time.sleep(0.1)
    raise TimeoutError(f"fleet_worker on port {port} did not become healthy in time")


def _start_worker(robot_type: str, port: int, robot_id: str, **extra_env: str) -> subprocess.Popen:
    env = {
        **os.environ,
        "ROBOT_TYPE": robot_type,
        "ROBOT_ID": robot_id,
        "WORKER_PORT": str(port),
        **extra_env,
    }
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "fleet_worker" / "app.py")],
        cwd=str(ROOT),
        env=env,
        # DEVNULL, not PIPE -- nothing in this fixture ever reads the
        # child's output, and an unread PIPE fills its OS buffer once
        # Flask/werkzeug's startup logging exceeds it, blocking the
        # child's write() before it ever reaches app.run().
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_health(port)
    return proc


def _stop_worker(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def mockrobot_worker():
    proc = _start_worker("mockrobot", 6501, "test-mockrobot-01", MOCK_PAYLOAD="visual_camera,thermal_camera")
    yield "test-mockrobot-01", "http://127.0.0.1:6501"
    _stop_worker(proc)


@pytest.fixture
def cloudfleet_worker():
    proc = _start_worker("cloudfleet", 6502, "test-cloudfleet-01", CLOUD_SENSORS="rgb_cam")
    yield "test-cloudfleet-01", "http://127.0.0.1:6502"
    _stop_worker(proc)


def test_get_capabilities_reflects_real_worker_payload(mockrobot_worker):
    robot_id, base_url = mockrobot_worker
    adapter = RemoteRobotAdapter(robot_id=robot_id, base_url=base_url)

    capabilities = adapter.get_capabilities()

    capability_types = {c.capability_type for c in capabilities}
    assert capability_types == {CapabilityType.NAVIGATE, CapabilityType.INSPECT_VISUAL, CapabilityType.INSPECT_THERMAL}


def test_get_capabilities_reflects_different_oem_worker(cloudfleet_worker):
    robot_id, base_url = cloudfleet_worker
    adapter = RemoteRobotAdapter(robot_id=robot_id, base_url=base_url)

    capabilities = adapter.get_capabilities()

    capability_types = {c.capability_type for c in capabilities}
    assert CapabilityType.INSPECT_VISUAL in capability_types


def test_execute_step_and_poll_to_completed_over_real_http(mockrobot_worker):
    robot_id, base_url = mockrobot_worker
    adapter = RemoteRobotAdapter(robot_id=robot_id, base_url=base_url)
    step = MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp1")

    started = adapter.execute_mission_step("m-remote-1", 0, step)
    polled = adapter.get_status("m-remote-1", 0)

    assert started.status == MissionStatus.COMPLETED
    assert polled.status == MissionStatus.COMPLETED
    assert polled.mission_id == "m-remote-1"


def test_handle_exception_and_estop_do_not_raise_over_real_http(mockrobot_worker):
    robot_id, base_url = mockrobot_worker
    adapter = RemoteRobotAdapter(robot_id=robot_id, base_url=base_url)
    step = MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp1")
    adapter.execute_mission_step("m-remote-2", 0, step)

    adapter.handle_exception("m-remote-2", 0, "operator abort")  # must not raise
    adapter.estop()  # must not raise


def test_unreachable_worker_returns_connectivity_lost_not_an_exception():
    # No fixture -- this base_url has no process behind it at all.
    adapter = RemoteRobotAdapter(robot_id="ghost-robot", base_url="http://127.0.0.1:6599", request_timeout_s=1.0)
    step = MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp1")

    status = adapter.execute_mission_step("m-remote-3", 0, step)

    assert status.status == MissionStatus.FAILED
    assert status.failure_reason == FailureReason.CONNECTIVITY_LOST


def test_unreachable_worker_get_capabilities_returns_empty_not_an_exception():
    adapter = RemoteRobotAdapter(robot_id="ghost-robot", base_url="http://127.0.0.1:6599", request_timeout_s=1.0)

    assert adapter.get_capabilities() == []


def test_get_power_pct_reads_real_worker_state(mockrobot_worker):
    robot_id, base_url = mockrobot_worker
    adapter = RemoteRobotAdapter(robot_id=robot_id, base_url=base_url)

    assert adapter.get_power_pct() == 100.0


def test_set_offline_round_trips_through_real_worker(mockrobot_worker):
    robot_id, base_url = mockrobot_worker
    adapter = RemoteRobotAdapter(robot_id=robot_id, base_url=base_url)

    adapter.set_offline(True)
    profile = adapter.get_fault_profile()

    assert profile.offline is True


def test_set_fault_profile_full_round_trip_over_real_http(mockrobot_worker):
    robot_id, base_url = mockrobot_worker
    adapter = RemoteRobotAdapter(robot_id=robot_id, base_url=base_url)

    adapter.set_fault_profile(FaultProfile(
        offline=False,
        flaky_failures_remaining=3,
        extra_latency_polls=2,
        failed_sensors={CapabilityType.INSPECT_THERMAL},
        random_failure_rates={FailureReason.NETWORK_DEGRADED: 0.5},
    ))

    profile = adapter.get_fault_profile()

    assert profile.flaky_failures_remaining == 3
    assert profile.extra_latency_polls == 2
    assert profile.failed_sensors == {CapabilityType.INSPECT_THERMAL}
    assert profile.random_failure_rates == {FailureReason.NETWORK_DEGRADED: 0.5}


def test_set_sensor_failed_affects_real_worker_execution(mockrobot_worker):
    robot_id, base_url = mockrobot_worker
    adapter = RemoteRobotAdapter(robot_id=robot_id, base_url=base_url)

    adapter.set_sensor_failed(CapabilityType.INSPECT_THERMAL, True)
    step = MissionStep(capability_type=CapabilityType.INSPECT_THERMAL, target="wp1")
    status = adapter.execute_mission_step("m-sensor-fail", 0, step)

    assert status.status == MissionStatus.FAILED
    assert status.failure_reason == FailureReason.SENSOR_FAULT


def test_unreachable_worker_get_fault_profile_returns_healthy_default():
    adapter = RemoteRobotAdapter(robot_id="ghost-robot", base_url="http://127.0.0.1:6599", request_timeout_s=1.0)

    profile = adapter.get_fault_profile()

    assert profile.offline is False
    assert profile.flaky_failures_remaining == 0


def test_unreachable_worker_get_power_pct_returns_zero_not_an_exception():
    adapter = RemoteRobotAdapter(robot_id="ghost-robot", base_url="http://127.0.0.1:6599", request_timeout_s=1.0)

    assert adapter.get_power_pct() == 0.0
