"""The conformance suite every OEM adapter must pass.

This is the mechanism that actually prevents "messy one-off drivers" --
not the abstract base class by itself (nothing stops someone from
technically implementing five methods that don't behave consistently),
but this suite running, unmodified, against every adapter before it's
trusted with a real robot.

Parametrized across TWO genuinely different OEM adapters:
  - MockRobotAdapter   (mock_robot_node.py)    -- local/ROS2-style, completes
                                                   a step synchronously
  - CloudFleetAdapter  (cloud_fleet_adapter.py) -- cloud/REST-style, completes
                                                   a step only after polling

Every test function in this file runs against both, unchanged. That's the
actual proof that the contract -- not any one adapter's internals -- is
what the platform depends on.
"""

from __future__ import annotations

import pytest

from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.mission import FailureReason, MissionStatus, MissionStep
from mock_robot.cloud_fleet_adapter import CloudFleetAdapter
from mock_robot.mock_robot_node import MockRobotAdapter


def poll_until_terminal(adapter, mission_id: str, step_index: int, max_polls: int = 5):
    """The same poll loop OrchestrationService.wait_for_completion() uses.
    Required here because the two OEMs resolve a step on different
    timelines (immediately vs. after a couple of polls) -- a conformance
    test that only checked the first status would pass against one OEM
    and silently miss a broken polling path on the other."""
    status = adapter.get_status(mission_id, step_index)
    polls = 1
    terminal = {MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.ABORTED}
    while status.status not in terminal and polls < max_polls:
        status = adapter.get_status(mission_id, step_index)
        polls += 1
    return status


def make_mock_visual() -> MockRobotAdapter:
    return MockRobotAdapter(robot_id="mock-visual", payload={"visual_camera"})


def make_cloud_visual() -> CloudFleetAdapter:
    return CloudFleetAdapter(robot_id="cloud-visual", fleet_manifest={"sensors": ["rgb_cam"]})


def make_mock_bare() -> MockRobotAdapter:
    return MockRobotAdapter(robot_id="mock-bare", payload=set())


def make_cloud_bare() -> CloudFleetAdapter:
    return CloudFleetAdapter(robot_id="cloud-bare", fleet_manifest={"sensors": []})


# Each test in the classes below runs once per entry here. Adding a third
# OEM adapter to this suite means adding one factory pair, not duplicating
# any test logic.
VISUAL_ADAPTER_FACTORIES = [make_mock_visual, make_cloud_visual]
BARE_ADAPTER_FACTORIES = [make_mock_bare, make_cloud_bare]


@pytest.fixture(params=VISUAL_ADAPTER_FACTORIES, ids=["mock_oem", "cloud_oem"])
def visual_adapter(request):
    return request.param()


@pytest.fixture(params=BARE_ADAPTER_FACTORIES, ids=["mock_oem", "cloud_oem"])
def bare_adapter(request):
    return request.param()


class TestCapabilityAdvertisement:
    def test_advertises_navigate_regardless_of_payload(self, bare_adapter):
        capability_types = {c.capability_type for c in bare_adapter.get_capabilities()}
        assert CapabilityType.NAVIGATE in capability_types

    def test_advertises_capability_matching_payload(self, visual_adapter):
        capability_types = {c.capability_type for c in visual_adapter.get_capabilities()}
        assert CapabilityType.INSPECT_VISUAL in capability_types

    def test_does_not_advertise_capability_without_payload(self, bare_adapter):
        capability_types = {c.capability_type for c in bare_adapter.get_capabilities()}
        assert CapabilityType.INSPECT_VISUAL not in capability_types
        assert CapabilityType.DETECT_GAS not in capability_types


class TestMissionStepExecution:
    def test_successful_step_eventually_reaches_completed(self, visual_adapter):
        """Deliberately does NOT assert on the status returned directly
        by execute_mission_step() -- per adapter_base.py, that call only
        has to return promptly with ASSIGNED/IN_PROGRESS. Whether a step
        resolves on that first call or after a few get_status() polls is
        an OEM implementation detail; only the eventual terminal status
        is part of the contract."""
        step = MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="waypoint_1")

        visual_adapter.execute_mission_step("mission-1", 0, step)
        final = poll_until_terminal(visual_adapter, "mission-1", 0)

        assert final.mission_id == "mission-1"
        assert final.step_index == 0
        assert final.status == MissionStatus.COMPLETED
        assert final.failure_reason == FailureReason.NONE

    def test_get_status_for_unknown_step_reports_failure_not_exception(self, visual_adapter):
        """A step this adapter never saw must come back as a normalized
        failure, not raise -- the mission executor treats an unhandled
        exception here as an adapter bug, not a mission failure."""
        status = visual_adapter.get_status("never-assigned", 0)

        assert status.status == MissionStatus.FAILED
        assert status.failure_reason == FailureReason.ADAPTER_UNRESPONSIVE


class TestSafetyPreconditions:
    def test_low_power_aborts_rather_than_attempting_step(self, visual_adapter):
        """Each OEM exposes its own power concept internally (battery_pct
        vs. soc_percent) -- this is the one place the test has to know
        which concrete adapter it's holding, because draining power is a
        test-setup action, not part of the RobotAdapter contract itself."""
        if isinstance(visual_adapter, MockRobotAdapter):
            visual_adapter.set_battery(3.0)
        elif isinstance(visual_adapter, CloudFleetAdapter):
            visual_adapter.set_state_of_charge(3.0)

        step = MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="waypoint_1")
        visual_adapter.execute_mission_step("mission-2", 0, step)
        final = poll_until_terminal(visual_adapter, "mission-2", 0)

        assert final.status == MissionStatus.FAILED
        assert final.failure_reason == FailureReason.LOW_BATTERY_ABORT

    def test_estop_is_callable_without_prior_state(self, visual_adapter):
        """estop() must work even if the adapter has never executed a
        step -- it cannot assume prior mission state exists."""
        visual_adapter.estop()  # must not raise

        assert getattr(visual_adapter, "estop_called", False) is True


class TestExceptionHandling:
    def test_handle_exception_on_completed_step_does_not_raise(self, visual_adapter):
        """Must be safe to call even after a step already finished on its
        own -- e.g. an intervention request racing a step's own
        completion."""
        step = MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="waypoint_1")
        visual_adapter.execute_mission_step("mission-3", 0, step)
        poll_until_terminal(visual_adapter, "mission-3", 0)

        visual_adapter.handle_exception("mission-3", 0, "operator requested pause")  # must not raise

    def test_handle_exception_on_unknown_step_does_not_raise(self, visual_adapter):
        visual_adapter.handle_exception("never-assigned", 0, "cleanup")  # must not raise


class TestOemErrorNormalization:
    def test_cloud_oem_error_code_is_mapped_to_shared_taxonomy(self):
        """OEM-specific proof: CloudFleetAdapter's own error vocabulary
        (ERR_PAYLOAD_FAULT) must never reach the caller directly -- only
        the normalized FailureReason. This has no equivalent on the mock
        local adapter because that OEM's error shape is different; each
        adapter gets its own normalization test for its own vocabulary,
        same as a real onboarding would require."""
        adapter = CloudFleetAdapter(
            robot_id="cloud-err", fleet_manifest={"sensors": ["rgb_cam"]}, inject_oem_error="ERR_PAYLOAD_FAULT"
        )
        step = MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="waypoint_1")

        adapter.execute_mission_step("mission-4", 0, step)
        final = poll_until_terminal(adapter, "mission-4", 0)

        assert final.status == MissionStatus.FAILED
        assert final.failure_reason == FailureReason.PRECONDITION_FAILED
        assert "ERR_PAYLOAD_FAULT" not in (final.detail or "")
