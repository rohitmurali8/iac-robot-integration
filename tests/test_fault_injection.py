"""Unit-level tests for FaultProfile (mock_robot/fault_injection.py).

Covers the FaultProfile data structure in isolation, AND (below,
TestFaultProfileThroughAdapters) its integration into the two real OEM
adapters -- proving the fault checks are actually consulted inside
execute_mission_step()/get_status(), not just correct as a standalone
data structure.
"""

from __future__ import annotations

import pytest

from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.mission import FailureReason, MissionStatus, MissionStep
from mock_robot.cloud_fleet_adapter import CloudFleetAdapter
from mock_robot.fault_injection import FaultProfile, healthy_profile
from mock_robot.mock_robot_node import MockRobotAdapter


class TestIsSensorFailed:
    def test_capability_in_failed_set_is_reported_failed(self) -> None:
        profile = FaultProfile(failed_sensors={CapabilityType.INSPECT_THERMAL})
        assert profile.is_sensor_failed(CapabilityType.INSPECT_THERMAL) is True

    def test_capability_not_in_failed_set_is_reported_healthy(self) -> None:
        profile = FaultProfile(failed_sensors={CapabilityType.INSPECT_THERMAL})
        assert profile.is_sensor_failed(CapabilityType.NAVIGATE) is False

    def test_default_profile_has_no_failed_sensors(self) -> None:
        profile = healthy_profile()
        for capability in CapabilityType:
            assert profile.is_sensor_failed(capability) is False


class TestConsumeFlakyFailure:
    def test_decrements_and_returns_true_while_remaining(self) -> None:
        profile = FaultProfile(flaky_failures_remaining=2)
        assert profile.consume_flaky_failure() is True
        assert profile.flaky_failures_remaining == 1
        assert profile.consume_flaky_failure() is True
        assert profile.flaky_failures_remaining == 0

    def test_returns_false_once_exhausted(self) -> None:
        profile = FaultProfile(flaky_failures_remaining=1)
        assert profile.consume_flaky_failure() is True
        assert profile.consume_flaky_failure() is False
        # Stays exhausted -- doesn't go negative or flip back to True.
        assert profile.consume_flaky_failure() is False
        assert profile.flaky_failures_remaining == 0

    def test_zero_remaining_never_fires(self) -> None:
        profile = FaultProfile()
        assert profile.consume_flaky_failure() is False


class TestMaybeRandomFailure:
    def test_rate_zero_never_fires_across_many_calls(self) -> None:
        profile = FaultProfile(
            rng_seed=1, random_failure_rates={FailureReason.NETWORK_DEGRADED: 0.0}
        )
        for _ in range(1000):
            assert profile.maybe_random_failure() is None

    def test_rate_one_always_fires(self) -> None:
        profile = FaultProfile(
            rng_seed=1, random_failure_rates={FailureReason.NETWORK_DEGRADED: 1.0}
        )
        for _ in range(1000):
            assert profile.maybe_random_failure() is FailureReason.NETWORK_DEGRADED

    def test_no_rates_configured_never_fires(self) -> None:
        profile = healthy_profile()
        for _ in range(100):
            assert profile.maybe_random_failure() is None

    def test_same_seed_and_rates_produce_identical_sequences(self) -> None:
        # Determinism-despite-randomness proof: TWO independently
        # constructed FaultProfile instances with the same rng_seed and
        # same random_failure_rates must produce an identical sequence of
        # outcomes across repeated calls.
        rates = {FailureReason.NETWORK_DEGRADED: 0.5, FailureReason.SENSOR_FAULT: 0.3}
        profile_a = FaultProfile(rng_seed=42, random_failure_rates=dict(rates))
        profile_b = FaultProfile(rng_seed=42, random_failure_rates=dict(rates))

        sequence_a = [profile_a.maybe_random_failure() for _ in range(200)]
        sequence_b = [profile_b.maybe_random_failure() for _ in range(200)]

        assert sequence_a == sequence_b
        # Sanity: the seeded sequence isn't trivially all-None (rates > 0).
        assert any(outcome is not None for outcome in sequence_a)

    def test_different_seeds_can_diverge(self) -> None:
        rates = {FailureReason.NETWORK_DEGRADED: 0.5}
        profile_a = FaultProfile(rng_seed=1, random_failure_rates=dict(rates))
        profile_b = FaultProfile(rng_seed=2, random_failure_rates=dict(rates))

        sequence_a = [profile_a.maybe_random_failure() for _ in range(200)]
        sequence_b = [profile_b.maybe_random_failure() for _ in range(200)]

        assert sequence_a != sequence_b


class TestHealthyProfileDefaults:
    def test_healthy_profile_is_fully_healthy(self) -> None:
        profile = healthy_profile()
        assert profile.offline is False
        assert profile.extra_latency_polls == 0
        assert profile.flaky_failures_remaining == 0
        assert profile.failed_sensors == set()
        assert profile.random_failure_rates == {}


def poll_until_terminal(adapter, mission_id: str, step_index: int, max_polls: int = 10):
    """Mirrors tests/test_adapter_contract.py's helper of the same name --
    duplicated locally rather than imported, since this file should stand
    alone and the conformance suite file must not gain a new consumer."""
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


# Parametrized across both real OEM adapters, same pattern as
# test_adapter_contract.py -- these tests prove the fault checks are wired
# into the REAL adapters' execute_mission_step()/get_status(), not just
# correct in isolation on the FaultProfile data structure above.
ADAPTER_FACTORIES = [make_mock_visual, make_cloud_visual]


@pytest.fixture(params=ADAPTER_FACTORIES, ids=["mock_oem", "cloud_oem"])
def adapter(request):
    return request.param()


class TestFaultProfileThroughAdapters:
    def test_offline_adapter_fails_new_step_with_connectivity_lost(self, adapter) -> None:
        adapter.set_offline(True)
        step = MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="waypoint_1")

        adapter.execute_mission_step("mission-offline", 0, step)
        final = poll_until_terminal(adapter, "mission-offline", 0)

        assert final.status == MissionStatus.FAILED
        assert final.failure_reason == FailureReason.CONNECTIVITY_LOST

    def test_sensor_failure_fails_only_the_affected_capability(self, adapter) -> None:
        adapter.set_sensor_failed(CapabilityType.INSPECT_VISUAL, True)

        visual_step = MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="waypoint_1")
        adapter.execute_mission_step("mission-sensor", 0, visual_step)
        visual_final = poll_until_terminal(adapter, "mission-sensor", 0)

        assert visual_final.status == MissionStatus.FAILED
        assert visual_final.failure_reason == FailureReason.SENSOR_FAULT

        # Same adapter instance, different capability -- proves the
        # toggle is capability-scoped, not a blanket adapter-wide outage.
        nav_step = MissionStep(capability_type=CapabilityType.NAVIGATE, target="waypoint_2")
        adapter.execute_mission_step("mission-sensor", 1, nav_step)
        nav_final = poll_until_terminal(adapter, "mission-sensor", 1)

        assert nav_final.status == MissionStatus.COMPLETED

    def test_flaky_failures_resolve_deterministically_after_n_attempts(self, adapter) -> None:
        # No retry mechanism is wired up yet (later task), so each
        # "attempt" here uses its own step_index. The invariant under test
        # is that consume_flaky_failure()'s countdown decrements across
        # calls to execute_mission_step() regardless of step_index, and
        # resolves to normal behavior once exhausted.
        adapter.set_flaky(2)
        step = MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="waypoint_1")

        adapter.execute_mission_step("mission-flaky", 0, step)
        attempt_1 = poll_until_terminal(adapter, "mission-flaky", 0)
        assert attempt_1.status == MissionStatus.FAILED
        assert attempt_1.failure_reason == FailureReason.NETWORK_DEGRADED

        adapter.execute_mission_step("mission-flaky", 1, step)
        attempt_2 = poll_until_terminal(adapter, "mission-flaky", 1)
        assert attempt_2.status == MissionStatus.FAILED
        assert attempt_2.failure_reason == FailureReason.NETWORK_DEGRADED

        adapter.execute_mission_step("mission-flaky", 2, step)
        attempt_3 = poll_until_terminal(adapter, "mission-flaky", 2)
        assert attempt_3.status == MissionStatus.COMPLETED
        assert attempt_3.failure_reason == FailureReason.NONE

    def test_extra_latency_polls_delays_terminal_status_by_exactly_n_polls(self, adapter) -> None:
        adapter.set_fault_profile(FaultProfile(extra_latency_polls=2))
        step = MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="waypoint_1")

        adapter.execute_mission_step("mission-latency", 0, step)

        # CloudFleetAdapter already has 1 "real" pending poll before this
        # feature existed; MockRobotAdapter resolves synchronously so all
        # IN_PROGRESS polls here come purely from the new mechanism.
        expected_in_progress_polls = 2
        if isinstance(adapter, CloudFleetAdapter):
            expected_in_progress_polls += 1

        for _ in range(expected_in_progress_polls):
            status = adapter.get_status("mission-latency", 0)
            assert status.status == MissionStatus.IN_PROGRESS

        final = adapter.get_status("mission-latency", 0)
        assert final.status == MissionStatus.COMPLETED
        assert final.failure_reason == FailureReason.NONE

    def test_default_fault_profile_matches_pre_existing_behavior(self) -> None:
        # Explicit regression pin, inlined from
        # test_adapter_contract.py's test_successful_step_eventually_reaches_completed --
        # constructing each adapter with NO fault_profile arg must behave
        # exactly as before this feature existed.
        for factory in ADAPTER_FACTORIES:
            fresh_adapter = factory()
            step = MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="waypoint_1")

            fresh_adapter.execute_mission_step("mission-default", 0, step)
            final = poll_until_terminal(fresh_adapter, "mission-default", 0)

            assert final.mission_id == "mission-default"
            assert final.step_index == 0
            assert final.status == MissionStatus.COMPLETED
            assert final.failure_reason == FailureReason.NONE
