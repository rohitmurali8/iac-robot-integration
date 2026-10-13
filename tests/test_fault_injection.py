"""Unit-level tests for FaultProfile (mock_robot/fault_injection.py).

Covers only the FaultProfile data structure in isolation -- a later task
wires this into the real OEM adapters and tests that integration.
"""

from __future__ import annotations

from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.mission import FailureReason
from mock_robot.fault_injection import FaultProfile, healthy_profile


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
