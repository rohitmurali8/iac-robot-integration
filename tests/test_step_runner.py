"""Tests for StepRunner -- single-step mission execution owning its own
retries.

Tested entirely against a minimal stub adapter (ScriptedAdapter, defined
below), never against MockRobotAdapter/CloudFleetAdapter -- the point of
StepRunner's isolation is that it is provably testable without the rest
of the system (registry, orchestration service, real adapters).
"""

from __future__ import annotations

from integration_layer.core.mission import FailureReason, MissionStatus, MissionStep, MissionStepStatus
from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.step_runner import StepRunner

MISSION_ID = "mission-1"
STEP_INDEX = 0


class ScriptedAdapter:
    """Returns a pre-scripted sequence of MissionStepStatus results, one
    per call to execute_mission_step or get_status (whichever is called
    next) -- a minimal stand-in for a real adapter's state machine, built
    only for StepRunner's own unit tests. Does not subclass RobotAdapter;
    StepRunner only calls execute_mission_step/get_status, which this
    class implements directly."""

    def __init__(self, scripted_statuses: list[MissionStepStatus]):
        self._scripted = list(scripted_statuses)
        self.execute_calls = 0
        self.get_status_calls = 0

    def execute_mission_step(self, mission_id, step_index, step):
        self.execute_calls += 1
        return self._next()

    def get_status(self, mission_id, step_index):
        self.get_status_calls += 1
        return self._next()

    def _next(self):
        if not self._scripted:
            raise AssertionError("ScriptedAdapter ran out of scripted statuses -- test didn't script enough calls")
        return self._scripted.pop(0)


def _status(status: MissionStatus, failure_reason: FailureReason = FailureReason.NONE, detail: str = "") -> MissionStepStatus:
    return MissionStepStatus(
        mission_id=MISSION_ID,
        step_index=STEP_INDEX,
        status=status,
        failure_reason=failure_reason,
        detail=detail,
    )


def _step() -> MissionStep:
    return MissionStep(capability_type=CapabilityType.NAVIGATE, target="dock-1")


def test_immediate_success_needs_no_retry() -> None:
    # execute_mission_step (attempt 1 kickoff) -> IN_PROGRESS
    # get_status (first poll of attempt 1) -> COMPLETED
    adapter = ScriptedAdapter(
        [
            _status(MissionStatus.IN_PROGRESS),
            _status(MissionStatus.COMPLETED),
        ]
    )
    runner = StepRunner()

    result = runner.start(adapter, MISSION_ID, STEP_INDEX, _step())

    assert result.status == MissionStatus.COMPLETED
    assert adapter.execute_calls == 1


def test_on_progress_fires_for_every_poll_and_every_retry() -> None:
    # execute (attempt 1 kickoff) -> IN_PROGRESS
    # get_status (attempt 1 poll) -> FAILED/CONNECTIVITY_LOST
    # retry -> execute (attempt 2 kickoff) -> IN_PROGRESS
    # get_status (attempt 2 poll #1) -> IN_PROGRESS
    # get_status (attempt 2 poll #2) -> COMPLETED
    adapter = ScriptedAdapter(
        [
            _status(MissionStatus.IN_PROGRESS),
            _status(MissionStatus.FAILED, FailureReason.CONNECTIVITY_LOST),
            _status(MissionStatus.IN_PROGRESS),
            _status(MissionStatus.IN_PROGRESS),
            _status(MissionStatus.COMPLETED),
        ]
    )
    runner = StepRunner()
    progress: list[MissionStepStatus] = []

    result = runner.start(adapter, MISSION_ID, STEP_INDEX, _step(), on_progress=progress.append)

    assert result.status == MissionStatus.COMPLETED
    assert len(progress) > 2
    statuses = [s.status for s in progress]
    assert MissionStatus.FAILED in statuses
    assert MissionStatus.IN_PROGRESS in statuses
    assert statuses[-1] == MissionStatus.COMPLETED
    # The synthesized "retrying" IN_PROGRESS status carries detail text.
    retrying_entries = [s for s in progress if s.status == MissionStatus.IN_PROGRESS and "retrying" in s.detail]
    assert len(retrying_entries) == 1


def test_comms_failure_retries_with_backoff_and_eventually_completes() -> None:
    # COMMS strategy: max_attempts=4, backoff_s=(0.0, 1.0, 2.0).
    # Each attempt = one execute_mission_step kickoff + one get_status poll.
    # Fail attempts 1-3, succeed on attempt 4.
    adapter = ScriptedAdapter(
        [
            _status(MissionStatus.IN_PROGRESS),  # attempt 1 kickoff
            _status(MissionStatus.FAILED, FailureReason.CONNECTIVITY_LOST),  # attempt 1 poll
            _status(MissionStatus.IN_PROGRESS),  # attempt 2 kickoff
            _status(MissionStatus.FAILED, FailureReason.CONNECTIVITY_LOST),  # attempt 2 poll
            _status(MissionStatus.IN_PROGRESS),  # attempt 3 kickoff
            _status(MissionStatus.FAILED, FailureReason.CONNECTIVITY_LOST),  # attempt 3 poll
            _status(MissionStatus.IN_PROGRESS),  # attempt 4 kickoff
            _status(MissionStatus.COMPLETED),  # attempt 4 poll
        ]
    )
    runner = StepRunner()

    result = runner.start(adapter, MISSION_ID, STEP_INDEX, _step())

    assert result.status == MissionStatus.COMPLETED
    assert adapter.execute_calls == 4


def test_comms_failure_exhausts_retries_and_returns_failed() -> None:
    # TIMEOUT is COMMS module: max_attempts=4, requires_operator=False.
    # Each attempt = one execute_mission_step kickoff + one get_status poll,
    # both FAILED (the kickoff's own return value is only ever emitted to
    # on_progress, never consulted for retry decisions -- only get_status's
    # terminal result is).
    scripted = []
    for _ in range(4):
        scripted.append(_status(MissionStatus.FAILED, FailureReason.TIMEOUT))  # kickoff
        scripted.append(_status(MissionStatus.FAILED, FailureReason.TIMEOUT))  # poll
    adapter = ScriptedAdapter(scripted)
    runner = StepRunner()

    result = runner.start(adapter, MISSION_ID, STEP_INDEX, _step())

    assert adapter.execute_calls == 4
    assert result.status == MissionStatus.FAILED
    assert result.failure_reason == FailureReason.TIMEOUT


def test_sensor_failure_never_retries_and_pauses_for_intervention() -> None:
    # SENSOR strategy: retryable=False, max_attempts=1, requires_operator=True.
    adapter = ScriptedAdapter(
        [
            _status(MissionStatus.FAILED, FailureReason.SENSOR_FAULT),  # kickoff
            _status(MissionStatus.FAILED, FailureReason.SENSOR_FAULT),  # poll
        ]
    )
    runner = StepRunner()

    result = runner.start(adapter, MISSION_ID, STEP_INDEX, _step())

    assert adapter.execute_calls == 1
    assert result.status == MissionStatus.PAUSED_FOR_INTERVENTION
    assert result.failure_reason == FailureReason.SENSOR_FAULT


def test_power_failure_never_retries_and_fails_outright_not_paused() -> None:
    # POWER strategy: retryable=False, max_attempts=1, requires_operator=False.
    adapter = ScriptedAdapter(
        [
            _status(MissionStatus.FAILED, FailureReason.LOW_BATTERY_ABORT),  # kickoff
            _status(MissionStatus.FAILED, FailureReason.LOW_BATTERY_ABORT),  # poll
        ]
    )
    runner = StepRunner()

    result = runner.start(adapter, MISSION_ID, STEP_INDEX, _step())

    assert adapter.execute_calls == 1
    assert result.status == MissionStatus.FAILED
    assert result.failure_reason == FailureReason.LOW_BATTERY_ABORT


def test_max_polls_budget_is_per_attempt_not_per_whole_call() -> None:
    # Single attempt that never resolves: get_status keeps returning
    # IN_PROGRESS forever. With max_polls=3, poll_to_terminal_with_retry
    # must give up after exactly 3 get_status calls and return the
    # still-IN_PROGRESS status (mirrors existing wait_for_completion
    # give-up behavior).
    adapter = ScriptedAdapter([_status(MissionStatus.IN_PROGRESS) for _ in range(3)])
    runner = StepRunner(max_polls=3)

    result = runner.poll_to_terminal_with_retry(adapter, MISSION_ID, STEP_INDEX, _step())

    assert adapter.get_status_calls == 3
    assert result.status == MissionStatus.IN_PROGRESS


def test_poll_to_terminal_with_retry_is_resumable_without_a_fresh_execute_call() -> None:
    # First scripted result (for the first get_status() call) is already
    # COMPLETED -- proves the method polls first rather than unconditionally
    # calling execute_mission_step itself.
    adapter = ScriptedAdapter([_status(MissionStatus.COMPLETED)])
    runner = StepRunner()

    result = runner.poll_to_terminal_with_retry(adapter, MISSION_ID, STEP_INDEX, _step())

    assert result.status == MissionStatus.COMPLETED
    assert adapter.execute_calls == 0
    assert adapter.get_status_calls == 1
