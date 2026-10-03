"""Second OEM adapter: a cloud/REST-style fleet API, not a local ROS2 robot.

This exists specifically to prove the adapter pattern absorbs genuinely
different integration shapes, not just two instances of the same mock.
Where MockRobotAdapter (mock_robot_node.py) represents a local, ROS2-style
OEM -- synchronous-ish calls, topic-driven capability discovery, battery
as a simple percentage -- this adapter represents the other common shape
in this space: a cloud fleet API with session-based auth, async job
submission + polling, and its own vendor-specific error vocabulary.

Every OEM quirk below (session tokens, job polling, "soc_percent" instead
of "battery_pct", ERR_* error codes) is invented and stays entirely
inside this file. Nothing about it is visible through the RobotAdapter
interface -- that's the point being demonstrated.
"""

from __future__ import annotations

import time
import uuid

from integration_layer.core.adapter_base import RobotAdapter
from integration_layer.core.capability_model import CapabilityDescriptor, CapabilityType
from integration_layer.core.mission import FailureReason, MissionStatus, MissionStep, MissionStepStatus

# Capability names as this fictional OEM's cloud API would actually name
# them in a fleet manifest response -- deliberately not matching our
# CapabilityType names, to prove the mapping lives in the adapter.
_OEM_SENSOR_TO_CAPABILITY = {
    "rgb_cam": CapabilityType.INSPECT_VISUAL,
    "ir_cam": CapabilityType.INSPECT_THERMAL,
    "voc_sensor": CapabilityType.DETECT_GAS,
}

# This OEM's own error vocabulary, mapped into our FailureReason taxonomy
# inside _map_oem_error() -- orchestration core never sees "ERR_PAYLOAD_FAULT".
_OEM_ERROR_MAP = {
    "ERR_PAYLOAD_FAULT": FailureReason.PRECONDITION_FAILED,
    "ERR_LINK_DROPPED": FailureReason.CONNECTIVITY_LOST,
    "ERR_SOC_CRITICAL": FailureReason.LOW_BATTERY_ABORT,
}


class CloudFleetAdapter(RobotAdapter):
    """Adapter for a fictional cloud fleet API. Mission steps become async
    'jobs' on this OEM's side -- execute_mission_step() submits a job and
    returns immediately (per the adapter contract); get_status() polls it,
    simulating the latency a real cloud round-trip would have."""

    def __init__(
        self,
        robot_id: str | None = None,
        fleet_manifest: dict | None = None,
        inject_oem_error: str | None = None,
    ) -> None:
        self.robot_id = robot_id or f"cloud-{uuid.uuid4().hex[:8]}"
        # Simulates the OEM's own login flow -- a real adapter would do
        # this against the vendor's actual auth endpoint at construction
        # or on first use.
        self._session_token = f"sess_{uuid.uuid4().hex[:12]}"
        self._manifest = fleet_manifest or {"sensors": ["rgb_cam"]}
        self._soc_percent = 100.0
        self._inject_oem_error = inject_oem_error
        # (mission_id, step_index) -> {"state", "polls_remaining", "oem_error",
        # "forced_status"}. forced_status short-circuits get_status() so that
        # a step rejected immediately (e.g. low power) is still consistently
        # reportable on every subsequent poll -- not just on the call that
        # rejected it. Every terminal path writes an entry here, with no
        # silent gaps, which is exactly what the conformance suite checks for.
        self._jobs: dict[tuple[str, int], dict] = {}
        self.estop_called = False

    def get_capabilities(self) -> list[CapabilityDescriptor]:
        capabilities = [CapabilityDescriptor(capability_type=CapabilityType.NAVIGATE)]
        for oem_sensor in self._manifest.get("sensors", []):
            capability_type = _OEM_SENSOR_TO_CAPABILITY.get(oem_sensor)
            if capability_type is not None:
                capabilities.append(
                    CapabilityDescriptor(
                        capability_type=capability_type,
                        required_payload=oem_sensor,
                    )
                )
        return capabilities

    def execute_mission_step(
        self, mission_id: str, step_index: int, step: MissionStep
    ) -> MissionStepStatus:
        key = (mission_id, step_index)

        if self._soc_percent <= 5.0:
            status = self._status(mission_id, step_index, MissionStatus.FAILED, FailureReason.LOW_BATTERY_ABORT)
            # Recorded here too, not just returned -- a step rejected
            # immediately must still be consistently reportable on every
            # later get_status() poll, the same as a step that resolves
            # through the normal job-polling path below.
            self._jobs[key] = {"state": "REJECTED", "forced_status": status}
            return status

        job_id = self._submit_job(step)
        self._jobs[key] = {
            "job_id": job_id,
            "state": "PENDING",
            # Simulates a cloud round-trip taking a couple of polls to
            # resolve -- this is what makes get_status() meaningfully
            # different from the local adapter's immediate-completion style.
            "polls_remaining": 1,
            "oem_error": self._inject_oem_error,
            "forced_status": None,
        }
        return self._status(mission_id, step_index, MissionStatus.IN_PROGRESS)

    def get_status(self, mission_id: str, step_index: int) -> MissionStepStatus:
        key = (mission_id, step_index)
        job = self._jobs.get(key)
        if job is None:
            return self._status(
                mission_id, step_index, MissionStatus.FAILED, FailureReason.ADAPTER_UNRESPONSIVE
            )

        if job.get("forced_status") is not None:
            return job["forced_status"]

        if job["oem_error"] is not None:
            job["state"] = "ERROR"
            reason = _map_oem_error(job["oem_error"])
            return self._status(mission_id, step_index, MissionStatus.FAILED, reason)

        if job["polls_remaining"] > 0:
            job["polls_remaining"] -= 1
            job["state"] = "RUNNING"
            return self._status(mission_id, step_index, MissionStatus.IN_PROGRESS)

        job["state"] = "DONE"
        self._soc_percent -= 3.0
        return self._status(mission_id, step_index, MissionStatus.COMPLETED)

    def handle_exception(self, mission_id: str, step_index: int, reason: str) -> None:
        key = (mission_id, step_index)
        job = self._jobs.get(key)
        if job is not None:
            job["state"] = "CANCELLED"

    def estop(self) -> None:
        # In a real adapter this would be a REST call to the OEM's
        # emergency-stop endpoint; recorded as a flag here for testability,
        # same observable effect as the local adapter's estop() despite
        # the completely different transport underneath.
        self.estop_called = True

    # -- test/demo helpers ----------------------------------------------

    def set_state_of_charge(self, pct: float) -> None:
        self._soc_percent = pct

    def _submit_job(self, step: MissionStep) -> str:
        return f"job_{uuid.uuid4().hex[:10]}"

    def _status(
        self,
        mission_id: str,
        step_index: int,
        status: MissionStatus,
        failure_reason: FailureReason = FailureReason.NONE,
        detail: str = "",
    ) -> MissionStepStatus:
        return MissionStepStatus(
            mission_id=mission_id,
            step_index=step_index,
            status=status,
            failure_reason=failure_reason,
            detail=detail,
            timestamp_unix_ms=int(time.time() * 1000),
        )


def _map_oem_error(oem_error: str) -> FailureReason:
    return _OEM_ERROR_MAP.get(oem_error, FailureReason.ADAPTER_UNRESPONSIVE)
