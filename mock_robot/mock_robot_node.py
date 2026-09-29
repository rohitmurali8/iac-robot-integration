"""Simulated robot adapter, no ROS2 or hardware required.

Implements the same RobotAdapter contract as
integration_layer/adapters/ros2_generic/adapter.py, in pure Python. This
is what lets the conformance suite (tests/test_adapter_contract.py) and
Integration Layer development run without blocking on physical robot
availability, per DESIGN.md section 4's "Day 30" plan -- and it's the
thing that actually runs in this repo, unlike the ROS2/C++ code which
needs a real ROS2 environment.

Also useful for staging's "shadow mode": point the mission executor at a
MockRobotAdapter to exercise the full assign -> execute -> status ->
audit path before any real hardware is involved.
"""

from __future__ import annotations

import time
import uuid

from integration_layer.core.adapter_base import RobotAdapter
from integration_layer.core.capability_model import CapabilityDescriptor, CapabilityType
from integration_layer.core.mission import FailureReason, MissionStatus, MissionStep, MissionStepStatus


class MockRobotAdapter(RobotAdapter):
    """A configurable fake robot. Payload determines advertised
    capabilities, mirroring how the real ROS2 adapter introspects present
    topics rather than hard-coding a capability list."""

    def __init__(
        self,
        robot_id: str | None = None,
        payload: set[str] | None = None,
        fail_next_step: bool = False,
        step_duration_s: float = 0.0,
    ) -> None:
        self.robot_id = robot_id or f"mock-{uuid.uuid4().hex[:8]}"
        self._payload = payload or {"visual_camera"}
        self._fail_next_step = fail_next_step
        self._step_duration_s = step_duration_s
        self._battery_pct = 100.0
        self._steps: dict[tuple[str, int], MissionStepStatus] = {}

    def get_capabilities(self) -> list[CapabilityDescriptor]:
        capabilities = [CapabilityDescriptor(capability_type=CapabilityType.NAVIGATE)]
        if "visual_camera" in self._payload:
            capabilities.append(
                CapabilityDescriptor(
                    capability_type=CapabilityType.INSPECT_VISUAL,
                    required_payload="visual_camera",
                )
            )
        if "thermal_camera" in self._payload:
            capabilities.append(
                CapabilityDescriptor(
                    capability_type=CapabilityType.INSPECT_THERMAL,
                    required_payload="thermal_camera",
                )
            )
        if "gas_sensor" in self._payload:
            capabilities.append(
                CapabilityDescriptor(
                    capability_type=CapabilityType.DETECT_GAS,
                    required_payload="gas_sensor",
                )
            )
        return capabilities

    def execute_mission_step(
        self, mission_id: str, step_index: int, step: MissionStep
    ) -> MissionStepStatus:
        if self._battery_pct <= 5.0:
            status = self._make_status(
                mission_id, step_index, MissionStatus.FAILED, FailureReason.LOW_BATTERY_ABORT
            )
            self._steps[(mission_id, step_index)] = status
            return status

        if self._step_duration_s > 0:
            time.sleep(self._step_duration_s)

        if self._fail_next_step:
            self._fail_next_step = False
            status = self._make_status(
                mission_id, step_index, MissionStatus.FAILED, FailureReason.PRECONDITION_FAILED
            )
        else:
            status = self._make_status(mission_id, step_index, MissionStatus.COMPLETED)

        self._battery_pct -= 2.0
        self._steps[(mission_id, step_index)] = status
        return status

    def get_status(self, mission_id: str, step_index: int) -> MissionStepStatus:
        key = (mission_id, step_index)
        if key in self._steps:
            return self._steps[key]
        return self._make_status(
            mission_id, step_index, MissionStatus.FAILED, FailureReason.ADAPTER_UNRESPONSIVE
        )

    def handle_exception(self, mission_id: str, step_index: int, reason: str) -> None:
        status = self._make_status(
            mission_id, step_index, MissionStatus.ABORTED, FailureReason.OPERATOR_INTERVENTION_REQUESTED, detail=reason
        )
        self._steps[(mission_id, step_index)] = status

    def estop(self) -> None:
        # No hardware to stop; recorded so tests can assert it was called.
        self.estop_called = True

    # -- test/simulation helpers ------------------------------------------

    def set_battery(self, pct: float) -> None:
        self._battery_pct = pct

    def _make_status(
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
        )
