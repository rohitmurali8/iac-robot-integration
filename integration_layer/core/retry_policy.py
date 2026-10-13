"""Module-aware retry policy.

Maps every FailureReason to the SystemModule that's actually implicated,
then maps every SystemModule to a RetryStrategy. OrchestrationService
(via StepRunner, added in a later task) consults this -- never an ad hoc
if/elif chain -- to decide whether a failed mission step should be
retried, and with what backoff.
"""

from __future__ import annotations

from dataclasses import dataclass

from integration_layer.core.mission import FailureReason, SystemModule


@dataclass(frozen=True)
class RetryStrategy:
    retryable: bool
    max_attempts: int  # total attempts including the first; 1 == "never retry"
    backoff_s: tuple[float, ...] = ()  # backoff before attempt 2, 3, ...; repeats last value past its length
    requires_operator: bool = False  # exhausting retries (or non-retryable) -> PAUSED_FOR_INTERVENTION, not just FAILED


# Every FailureReason must have an entry. Deliberately exhaustive rather
# than a .get(..., default) fallback -- a new FailureReason added later
# without an entry here should raise loudly (see classify_failure()), not
# silently inherit an unrelated default.
FAILURE_REASON_MODULE: dict[FailureReason, SystemModule] = {
    FailureReason.NONE: SystemModule.PLANNING,  # never consulted (NONE isn't a failure); included for exhaustiveness
    FailureReason.CONNECTIVITY_LOST: SystemModule.COMMS,
    FailureReason.TIMEOUT: SystemModule.COMMS,
    FailureReason.ADAPTER_UNRESPONSIVE: SystemModule.COMMS,
    FailureReason.NETWORK_DEGRADED: SystemModule.COMMS,
    FailureReason.SENSOR_FAULT: SystemModule.SENSOR,
    FailureReason.LOW_BATTERY_ABORT: SystemModule.POWER,
    FailureReason.SAFETY_ESTOP_TRIGGERED: SystemModule.SAFETY,
    FailureReason.OPERATOR_INTERVENTION_REQUESTED: SystemModule.SAFETY,
    FailureReason.PRECONDITION_FAILED: SystemModule.PLANNING,
}

# One strategy per SystemModule -- this is the "smart retry keyed by which
# module failed" requirement, as a literal, readable table.
MODULE_RETRY_STRATEGY: dict[SystemModule, RetryStrategy] = {
    SystemModule.COMMS: RetryStrategy(
        retryable=True, max_attempts=4, backoff_s=(0.0, 1.0, 2.0)
    ),  # transient network issue; worth retrying with backoff
    SystemModule.SENSOR: RetryStrategy(
        retryable=False, max_attempts=1, requires_operator=True
    ),  # retrying won't fix broken hardware; flag for operator instead of looping
    SystemModule.POWER: RetryStrategy(
        retryable=False, max_attempts=1
    ),  # retrying with low battery doesn't help; mission aborts, reassignment is orchestration core's job
    SystemModule.SAFETY: RetryStrategy(
        retryable=False, max_attempts=1, requires_operator=True
    ),  # never auto-retry past an e-stop/operator-intervention event
    SystemModule.PLANNING: RetryStrategy(
        retryable=False, max_attempts=1
    ),  # precondition failures are a logic/planning issue; retrying the identical step won't change the params that caused it
}


def classify_failure(reason: FailureReason) -> SystemModule:
    """Raises KeyError (loudly, not silently) if a FailureReason is ever
    added without a corresponding module classification."""
    return FAILURE_REASON_MODULE[reason]


def strategy_for(reason: FailureReason) -> RetryStrategy:
    return MODULE_RETRY_STRATEGY[classify_failure(reason)]


def backoff_for_attempt(strategy: RetryStrategy, attempt_number: int) -> float:
    """attempt_number is 1-indexed for the attempt about to be made (2nd
    attempt, 3rd attempt, ...). Returns 0.0 for the first attempt (no
    backoff before trying at all) and the configured backoff before each
    subsequent attempt, repeating the last configured value if
    max_attempts exceeds len(backoff_s)."""
    if attempt_number <= 1 or not strategy.backoff_s:
        return 0.0
    idx = min(attempt_number - 2, len(strategy.backoff_s) - 1)
    return strategy.backoff_s[idx]
