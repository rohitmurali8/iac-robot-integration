"""The conformance suite every OEM adapter must pass.

This is the mechanism that actually prevents "messy one-off drivers" --
not the abstract base class by itself (nothing stops someone from
technically implementing five methods that don't behave consistently),
but this suite running in CI against every adapter, including new ones,
before they're trusted with a real robot.

Runs against MockRobotAdapter here since that requires no ROS2/hardware
environment. In a real CI setup, Ros2GenericAdapter (and every other OEM
adapter) would be parametrized into the same test functions -- the point
of the suite is that it's the *same* tests for every adapter, not a
bespoke test file per robot.
"""

from __future__ import annotations

import pytest

from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.mission import FailureReason, MissionStatus, MissionStep
from mock_robot.mock_robot_node import MockRobotAdapter


def make_adapter(**kwargs) -> MockRobotAdapter:
    return MockRobotAdapter(**kwargs)


class TestCapabilityAdvertisement:
    def test_advertises_navigate_regardless_of_payload(self):
        adapter = make_adapter(payload=set())
        capability_types = {c.capability_type for c in adapter.get_capabilities()}
        assert CapabilityType.NAVIGATE in capability_types

    def test_advertises_capability_matching_payload(self):
        adapter = make_adapter(payload={"thermal_camera"})
        capability_types = {c.capability_type for c in adapter.get_capabilities()}
        assert CapabilityType.INSPECT_THERMAL in capability_types
        assert CapabilityType.INSPECT_VISUAL not in capability_types

    def test_does_not_advertise_capability_without_payload(self):
        adapter = make_adapter(payload={"visual_camera"})
        capability_types = {c.capability_type for c in adapter.get_capabilities()}
        assert CapabilityType.DETECT_GAS not in capability_types


class TestMissionStepExecution:
    def test_successful_step_returns_completed_status(self):
        adapter = make_adapter(payload={"visual_camera"})
        step = MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="waypoint_1")

        status = adapter.execute_mission_step("mission-1", 0, step)

        assert status.mission_id == "mission-1"
        assert status.step_index == 0
        assert status.status == MissionStatus.COMPLETED
        assert status.failure_reason == FailureReason.NONE

    def test_get_status_matches_execute_mission_step_result(self):
        adapter = make_adapter()
        step = MissionStep(capability_type=CapabilityType.NAVIGATE, target="waypoint_1")

        executed = adapter.execute_mission_step("mission-2", 0, step)
        polled = adapter.get_status("mission-2", 0)

        assert polled.status == executed.status
        assert polled.mission_id == executed.mission_id

    def test_get_status_for_unknown_step_reports_failure_not_exception(self):
        """A step this adapter never saw must come back as a normalized
        failure, not raise -- the mission executor treats an unhandled
        exception here as an adapter bug, not a mission failure."""
        adapter = make_adapter()

        status = adapter.get_status("never-assigned", 0)

        assert status.status == MissionStatus.FAILED
        assert status.failure_reason == FailureReason.ADAPTER_UNRESPONSIVE

    def test_failure_uses_normalized_taxonomy_not_raw_exception(self):
        """This is the core contract requirement from DESIGN.md section
        2 -- OEM-specific failures must be mapped to FailureReason before
        crossing the adapter boundary. An adapter that raises an
        OEM-specific exception here fails the suite."""
        adapter = make_adapter(fail_next_step=True)
        step = MissionStep(capability_type=CapabilityType.NAVIGATE, target="waypoint_1")

        status = adapter.execute_mission_step("mission-3", 0, step)

        assert status.status == MissionStatus.FAILED
        assert status.failure_reason != FailureReason.NONE


class TestSafetyPreconditions:
    def test_low_battery_aborts_rather_than_attempting_step(self):
        adapter = make_adapter()
        adapter.set_battery(3.0)
        step = MissionStep(capability_type=CapabilityType.NAVIGATE, target="waypoint_1")

        status = adapter.execute_mission_step("mission-4", 0, step)

        assert status.status == MissionStatus.FAILED
        assert status.failure_reason == FailureReason.LOW_BATTERY_ABORT

    def test_estop_is_callable_without_prior_state(self):
        """estop() must work even if the adapter has never executed a
        step -- it cannot assume prior mission state exists."""
        adapter = make_adapter()

        adapter.estop()  # must not raise

        assert getattr(adapter, "estop_called", False) is True


class TestExceptionHandling:
    def test_handle_exception_on_completed_step_does_not_raise(self):
        """Must be safe to call even after a step already finished on its
        own -- e.g. an intervention request racing a step's own
        completion."""
        adapter = make_adapter()
        step = MissionStep(capability_type=CapabilityType.NAVIGATE, target="waypoint_1")
        adapter.execute_mission_step("mission-5", 0, step)

        adapter.handle_exception("mission-5", 0, "operator requested pause")  # must not raise

    def test_handle_exception_on_unknown_step_does_not_raise(self):
        adapter = make_adapter()

        adapter.handle_exception("never-assigned", 0, "cleanup")  # must not raise
