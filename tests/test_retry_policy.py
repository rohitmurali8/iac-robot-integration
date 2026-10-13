"""Tests for the module-aware retry policy table.

Covers: exhaustiveness (every FailureReason classifies, every SystemModule
has a strategy), the exact (FailureReason, SystemModule, retryable) triples
from the design table, and backoff_for_attempt determinism/repeat-last-value
behavior.
"""

from __future__ import annotations

import pytest

from integration_layer.core.mission import FailureReason, SystemModule
from integration_layer.core.retry_policy import (
    MODULE_RETRY_STRATEGY,
    RetryStrategy,
    backoff_for_attempt,
    classify_failure,
    strategy_for,
)


class TestExhaustiveness:
    @pytest.mark.parametrize("reason", list(FailureReason))
    def test_every_failure_reason_classifies(self, reason: FailureReason) -> None:
        # Must not raise KeyError for any current FailureReason member.
        module = classify_failure(reason)
        assert isinstance(module, SystemModule)

    @pytest.mark.parametrize("module", list(SystemModule))
    def test_every_system_module_has_a_strategy(self, module: SystemModule) -> None:
        assert module in MODULE_RETRY_STRATEGY
        assert isinstance(MODULE_RETRY_STRATEGY[module], RetryStrategy)

    def test_unknown_failure_reason_raises_keyerror(self) -> None:
        # classify_failure must raise loudly, not silently fall back, for
        # anything not in the table. We fake an unregistered member by
        # passing a sentinel that isn't a FailureReason key in the dict.
        with pytest.raises(KeyError):
            classify_failure("not_a_real_failure_reason")  # type: ignore[arg-type]


# (FailureReason, expected SystemModule, expected retryable) per the design
# table in retry_policy.py.
EXPECTED_TRIPLES = [
    (FailureReason.NONE, SystemModule.PLANNING, False),
    (FailureReason.CONNECTIVITY_LOST, SystemModule.COMMS, True),
    (FailureReason.TIMEOUT, SystemModule.COMMS, True),
    (FailureReason.ADAPTER_UNRESPONSIVE, SystemModule.COMMS, True),
    (FailureReason.NETWORK_DEGRADED, SystemModule.COMMS, True),
    (FailureReason.SENSOR_FAULT, SystemModule.SENSOR, False),
    (FailureReason.LOW_BATTERY_ABORT, SystemModule.POWER, False),
    (FailureReason.SAFETY_ESTOP_TRIGGERED, SystemModule.SAFETY, False),
    (FailureReason.OPERATOR_INTERVENTION_REQUESTED, SystemModule.SAFETY, False),
    (FailureReason.PRECONDITION_FAILED, SystemModule.PLANNING, False),
]


class TestClassificationTable:
    @pytest.mark.parametrize("reason,expected_module,expected_retryable", EXPECTED_TRIPLES)
    def test_classification_and_retryability(
        self,
        reason: FailureReason,
        expected_module: SystemModule,
        expected_retryable: bool,
    ) -> None:
        module = classify_failure(reason)
        assert module is expected_module
        strategy = strategy_for(reason)
        assert strategy.retryable is expected_retryable

    def test_all_failure_reasons_covered_by_expected_triples(self) -> None:
        # Guards against the test table itself going stale if a new
        # FailureReason is added without updating EXPECTED_TRIPLES.
        covered = {reason for reason, _, _ in EXPECTED_TRIPLES}
        assert covered == set(FailureReason)


class TestBackoffForAttempt:
    def test_first_attempt_is_always_zero(self) -> None:
        strategy = MODULE_RETRY_STRATEGY[SystemModule.COMMS]
        assert backoff_for_attempt(strategy, 1) == 0.0

    def test_attempt_zero_or_negative_is_also_zero(self) -> None:
        strategy = MODULE_RETRY_STRATEGY[SystemModule.COMMS]
        assert backoff_for_attempt(strategy, 0) == 0.0
        assert backoff_for_attempt(strategy, -5) == 0.0

    @pytest.mark.parametrize(
        "attempt_number,expected",
        [
            (1, 0.0),  # first attempt, no backoff
            (2, 0.0),  # backoff_s[0]
            (3, 1.0),  # backoff_s[1]
            (4, 2.0),  # backoff_s[2]
            (5, 2.0),  # past len(backoff_s), repeats last value
            (6, 2.0),  # still repeats last value
        ],
    )
    def test_comms_strategy_backoff_sequence(
        self, attempt_number: int, expected: float
    ) -> None:
        strategy = MODULE_RETRY_STRATEGY[SystemModule.COMMS]
        assert strategy.backoff_s == (0.0, 1.0, 2.0)
        assert backoff_for_attempt(strategy, attempt_number) == expected

    def test_empty_backoff_tuple_always_zero(self) -> None:
        strategy = MODULE_RETRY_STRATEGY[SystemModule.SENSOR]
        assert strategy.backoff_s == ()
        for attempt in range(1, 5):
            assert backoff_for_attempt(strategy, attempt) == 0.0
