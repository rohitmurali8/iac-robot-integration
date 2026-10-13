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
from mock_robot.fault_injection import FaultProfile, healthy_profile


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
        fault_profile: FaultProfile | None = None,
    ) -> None:
        self.robot_id = robot_id or f"mock-{uuid.uuid4().hex[:8]}"
        # `payload if payload is not None else ...`, not `payload or ...` --
        # an explicitly-passed empty set() is falsy, and `or` would
        # silently override "this robot has no payload" with the default.
        self._payload = payload if payload is not None else {"visual_camera"}
        self._fail_next_step = fail_next_step
        self._step_duration_s = step_duration_s
        self._battery_pct = 100.0
        self._steps: dict[tuple[str, int], MissionStepStatus] = {}
        self._fault_profile = fault_profile if fault_profile is not None else healthy_profile()
        self._pending_latency: dict[tuple[str, int], int] = {}

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
        if self._fault_profile.offline:
            status = self._make_status(
                mission_id, step_index, MissionStatus.FAILED, FailureReason.CONNECTIVITY_LOST
            )
            self._steps[(mission_id, step_index)] = status
            return status

        if self._fault_profile.is_sensor_failed(step.capability_type):
            status = self._make_status(
                mission_id, step_index, MissionStatus.FAILED, FailureReason.SENSOR_FAULT
            )
            self._steps[(mission_id, step_index)] = status
            return status

        if self._fault_profile.consume_flaky_failure():
            status = self._make_status(
                mission_id, step_index, MissionStatus.FAILED, FailureReason.NETWORK_DEGRADED
            )
            self._steps[(mission_id, step_index)] = status
            return status

        random_failure = self._fault_profile.maybe_random_failure()
        if random_failure is not None:
            status = self._make_status(mission_id, step_index, MissionStatus.FAILED, random_failure)
            self._steps[(mission_id, step_index)] = status
            return status

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

        if status.status == MissionStatus.COMPLETED and self._fault_profile.extra_latency_polls > 0:
            self._pending_latency[(mission_id, step_index)] = self._fault_profile.extra_latency_polls

        return status

    def get_status(self, mission_id: str, step_index: int) -> MissionStepStatus:
        key = (mission_id, step_index)
        remaining = self._pending_latency.get(key, 0)
        if remaining > 0:
            self._pending_latency[key] = remaining - 1
            return self._make_status(mission_id, step_index, MissionStatus.IN_PROGRESS)
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

    def set_fault_profile(self, fault_profile: FaultProfile) -> None:
        self._fault_profile = fault_profile

    def get_fault_profile(self) -> FaultProfile:
        return self._fault_profile

    def set_sensor_failed(self, capability_type: CapabilityType, failed: bool = True) -> None:
        if failed:
            self._fault_profile.failed_sensors.add(capability_type)
        else:
            self._fault_profile.failed_sensors.discard(capability_type)

    def set_offline(self, offline: bool = True) -> None:
        self._fault_profile.offline = offline

    def set_flaky(self, failures_remaining: int) -> None:
        self._fault_profile.flaky_failures_remaining = failures_remaining

    def set_random_failure_rate(self, failure_reason: FailureReason, probability: float) -> None:
        self._fault_profile.random_failure_rates[failure_reason] = probability

    def get_power_pct(self) -> float:
        return self._battery_pct

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
