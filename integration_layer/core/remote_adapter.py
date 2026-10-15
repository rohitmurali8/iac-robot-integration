"""HTTP-backed RobotAdapter -- drives a robot worker pod
(fleet_worker/app.py) running in a Kubernetes cluster over the network,
instead of an in-process Python object. This is what lets
OrchestrationService dispatch missions to robots that are genuinely
separate processes (and, in a real deployment, genuinely separate
physical robots at the edge each running this same Docker image) --
the SAME RobotAdapter interface either way, so nothing above the
registry needs to know or care that a step's execution crossed a
network boundary.

Uses urllib (stdlib), not `requests`, matching this repo's existing
practice elsewhere (data_delivery.py's webhook push, webapp/app.py's
SSE handling) of not adding a new HTTP-client dependency when the
stdlib already does the job.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from integration_layer.core.adapter_base import RobotAdapter
from integration_layer.core.capability_model import CapabilityDescriptor, CapabilityType
from integration_layer.core.mission import FailureReason, MissionStatus, MissionStep, MissionStepStatus
from integration_layer.core.wire import (
    capability_from_dict,
    fault_profile_from_dict,
    fault_profile_to_dict,
    status_from_dict,
    step_to_dict,
)
from mock_robot.fault_injection import FaultProfile, healthy_profile


class RemoteRobotAdapter(RobotAdapter):
    """robot_id is the worker pod's own identity (its Kubernetes pod
    name); base_url is that pod's directly-addressable URL (its pod IP
    and fleet_worker's port -- see k8s/fleet_discovery.py, which is
    what actually constructs these from the Kubernetes Pods API)."""

    def __init__(self, robot_id: str, base_url: str, request_timeout_s: float = 5.0) -> None:
        self.robot_id = robot_id
        self._base_url = base_url.rstrip("/")
        self._timeout = request_timeout_s

    def get_capabilities(self) -> list[CapabilityDescriptor]:
        try:
            data = self._get("/capabilities")
        except (urllib.error.URLError, OSError, ValueError):
            # A pod that's momentarily unreachable advertises nothing
            # rather than raising -- AdapterRegistry.find_capable()
            # already treats "advertises nothing" as "not currently a
            # candidate," which is the correct behavior for a worker
            # that's mid-restart, not a reason to crash capability
            # resolution for the whole fleet.
            return []
        return [capability_from_dict(c) for c in data]

    def execute_mission_step(self, mission_id: str, step_index: int, step: MissionStep) -> MissionStepStatus:
        try:
            data = self._post(
                "/execute_step",
                {"mission_id": mission_id, "step_index": step_index, "step": step_to_dict(step)},
            )
            return status_from_dict(data)
        except (urllib.error.URLError, OSError, ValueError):
            return self._unreachable_status(mission_id, step_index)

    def get_status(self, mission_id: str, step_index: int) -> MissionStepStatus:
        try:
            data = self._get(f"/status?mission_id={mission_id}&step_index={step_index}")
            return status_from_dict(data)
        except (urllib.error.URLError, OSError, ValueError):
            return self._unreachable_status(mission_id, step_index)

    def handle_exception(self, mission_id: str, step_index: int, reason: str) -> None:
        try:
            self._post("/handle_exception", {"mission_id": mission_id, "step_index": step_index, "reason": reason})
        except (urllib.error.URLError, OSError, ValueError):
            pass  # best-effort -- the pod is already unreachable, nothing more to signal

    def estop(self) -> None:
        try:
            self._post("/estop", {})
        except (urllib.error.URLError, OSError, ValueError):
            pass

    # -- concrete-adapter convenience methods ---------------------------
    #
    # Not part of RobotAdapter -- both MockRobotAdapter and
    # CloudFleetAdapter expose this exact method set directly, and
    # webapp/app.py's fleet snapshot / fault-injection controls call
    # them on whatever adapter is registered without an isinstance
    # check. Proxying them here, against fleet_worker/app.py's matching
    # routes, is what lets that same webapp code work unmodified
    # against a Kubernetes-deployed fleet.

    def get_power_pct(self) -> float:
        try:
            data = self._get("/power")
            return data["power_pct"]
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            return 0.0  # unreachable pod reads as fully depleted, not a crash

    def get_fault_profile(self) -> FaultProfile:
        try:
            data = self._get("/fault_profile")
            return fault_profile_from_dict(data)
        except (urllib.error.URLError, OSError, ValueError):
            return healthy_profile()  # unreachable pod reads as healthy-but-stale, not a crash

    def set_fault_profile(self, fault_profile: FaultProfile) -> None:
        try:
            self._post("/fault_profile", fault_profile_to_dict(fault_profile))
        except (urllib.error.URLError, OSError, ValueError):
            pass

    def set_offline(self, offline: bool = True) -> None:
        profile = self.get_fault_profile()
        profile.offline = offline
        self.set_fault_profile(profile)

    def set_flaky(self, failures_remaining: int) -> None:
        profile = self.get_fault_profile()
        profile.flaky_failures_remaining = failures_remaining
        self.set_fault_profile(profile)

    def set_sensor_failed(self, capability_type: CapabilityType, failed: bool = True) -> None:
        profile = self.get_fault_profile()
        if failed:
            profile.failed_sensors.add(capability_type)
        else:
            profile.failed_sensors.discard(capability_type)
        self.set_fault_profile(profile)

    def set_random_failure_rate(self, failure_reason: FailureReason, probability: float) -> None:
        profile = self.get_fault_profile()
        profile.random_failure_rates[failure_reason] = probability
        self.set_fault_profile(profile)

    def _unreachable_status(self, mission_id: str, step_index: int) -> MissionStepStatus:
        # The worker pod itself is unreachable (crashed, scaled down
        # mid-mission, network partition) -- this IS a comms failure in
        # this codebase's own taxonomy, so it funnels into the exact
        # same FailureReason/retry_policy path a genuine robot
        # connectivity loss already does, rather than raising an
        # unhandled exception up through MissionExecutor.
        return MissionStepStatus(
            mission_id=mission_id,
            step_index=step_index,
            status=MissionStatus.FAILED,
            failure_reason=FailureReason.CONNECTIVITY_LOST,
            detail=f"worker pod unreachable: {self._base_url}",
        )

    def _get(self, path: str):
        request_obj = urllib.request.Request(f"{self._base_url}{path}", method="GET")
        with urllib.request.urlopen(request_obj, timeout=self._timeout) as response:
            return json.loads(response.read())

    def _post(self, path: str, body: dict):
        data = json.dumps(body).encode()
        request_obj = urllib.request.Request(
            f"{self._base_url}{path}", data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(request_obj, timeout=self._timeout) as response:
            return json.loads(response.read())
