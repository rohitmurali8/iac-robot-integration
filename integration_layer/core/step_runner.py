"""Single-step mission execution, including that step's own retries.

StepRunner owns no cross-step state -- a call is self-contained given an
adapter, step, and mission/step identifiers. This isolation is what
prevents a bug in retry handling from becoming a mission-sequencing bug:
StepRunner has no visibility into other steps or the rest of the mission
(that's MissionExecutor's job, added in a later task, which composes this
class but never reimplements its logic).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from integration_layer.core import retry_policy
from integration_layer.core.mission import MissionStatus, MissionStep, MissionStepStatus

ProgressCallback = Callable[[MissionStepStatus], None]

_TERMINAL = {
    MissionStatus.COMPLETED,
    MissionStatus.FAILED,
    MissionStatus.ABORTED,
    MissionStatus.PAUSED_FOR_INTERVENTION,
}


@dataclass
class StepRunner:
    max_polls: int = 10
    poll_interval_s: float = 0.0

    def start(
        self,
        adapter,
        mission_id: str,
        step_index: int,
        step: MissionStep,
        on_progress: ProgressCallback | None = None,
    ) -> MissionStepStatus:
        """Use for a step that hasn't been kicked off yet: calls
        execute_mission_step() once, then delegates to
        poll_to_terminal_with_retry()."""
        status = adapter.execute_mission_step(mission_id, step_index, step)
        self._emit(on_progress, status)
        return self.poll_to_terminal_with_retry(adapter, mission_id, step_index, step, on_progress)

    def poll_to_terminal_with_retry(
        self,
        adapter,
        mission_id: str,
        step_index: int,
        step: MissionStep,
        on_progress: ProgressCallback | None = None,
    ) -> MissionStepStatus:
        """Resumable: assumes execute_mission_step was already called at
        least once for this (mission_id, step_index) -- by start() above,
        or by a caller that already kicked the step off itself. Polls
        get_status() to terminal (on_progress fires after EVERY poll).
        On a FAILED terminal result, consults retry_policy.strategy_for()
        to decide: retry (sleep backoff only if poll_interval_s > 0,
        re-invoke execute_mission_step on the SAME step_index, poll the
        new attempt) vs. stop. On stopping a non-retryable or
        retries-exhausted failure whose strategy requires_operator,
        synthesizes and returns PAUSED_FOR_INTERVENTION -- adapters never
        produce this status themselves, only this method does."""
        attempt = 1
        status = self._poll_current_attempt_to_terminal(adapter, mission_id, step_index, on_progress)

        while status.status == MissionStatus.FAILED:
            strategy = retry_policy.strategy_for(status.failure_reason)

            if not strategy.retryable or attempt >= strategy.max_attempts:
                if strategy.requires_operator:
                    paused = MissionStepStatus(
                        mission_id=mission_id,
                        step_index=step_index,
                        status=MissionStatus.PAUSED_FOR_INTERVENTION,
                        failure_reason=status.failure_reason,
                        detail=status.detail,
                    )
                    self._emit(on_progress, paused)
                    return paused
                return status

            attempt += 1
            module = retry_policy.classify_failure(status.failure_reason)
            backoff = retry_policy.backoff_for_attempt(strategy, attempt)
            if self.poll_interval_s and backoff:
                time.sleep(backoff)

            retrying_status = MissionStepStatus(
                mission_id=mission_id,
                step_index=step_index,
                status=MissionStatus.IN_PROGRESS,
                failure_reason=status.failure_reason,
                detail=f"retrying (attempt {attempt}/{strategy.max_attempts}, module={module.value}, backoff_s={backoff})",
            )
            self._emit(on_progress, retrying_status)

            new_attempt_status = adapter.execute_mission_step(mission_id, step_index, step)
            self._emit(on_progress, new_attempt_status)

            status = self._poll_current_attempt_to_terminal(adapter, mission_id, step_index, on_progress)

        return status

    def _poll_current_attempt_to_terminal(
        self, adapter, mission_id: str, step_index: int, on_progress: ProgressCallback | None
    ) -> MissionStepStatus:
        """One attempt's own poll loop: fresh max_polls budget every
        time this is called (each retry attempt is functionally a new
        execute_mission_step call needing its own time to resolve)."""
        status = adapter.get_status(mission_id, step_index)
        self._emit(on_progress, status)
        polls = 1
        while status.status not in _TERMINAL and polls < self.max_polls:
            if self.poll_interval_s:
                time.sleep(self.poll_interval_s)
            status = adapter.get_status(mission_id, step_index)
            self._emit(on_progress, status)
            polls += 1
        return status

    def _emit(self, on_progress: ProgressCallback | None, status: MissionStepStatus) -> None:
        if on_progress is not None:
            on_progress(status)
