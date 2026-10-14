"""Cross-step mission sequencing.

MissionExecutor owns WHICH step is next, WHICH adapter serves it, and
WHEN the mission as a whole is done. It delegates ALL single-step
execution -- including retries -- to StepRunner, and only ever calls an
adapter's execute_mission_step()/get_status() directly for the raw,
non-blocking kickoff of step 0 (start_mission), matching
OrchestrationService.assign_mission()'s pinned "return promptly, don't
poll" contract. A bug in cross-step sequencing lives here, testable with
a stub StepRunner; it cannot hide inside per-step retry logic (StepRunner's
isolated concern, already tested in Task 4).

The moment a mission reaches ANY terminal outcome, this class records
the post-mission analytics outcome automatically (exactly once per
mission, even if continue_mission is called again afterward) -- this IS
the "post-mission automation" the plan calls for: no manual trigger, no
separate step.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from integration_layer.core import retry_policy
from integration_layer.core.adapter_base import RobotAdapter
from integration_layer.core.events import AuditEvent, AuditLog
from integration_layer.core.mission import (
    FailureReason,
    Mission,
    MissionStatus,
    MissionStep,
    MissionStepStatus,
    SystemModule,
)
from integration_layer.core.mission_analytics import MissionAnalytics
from integration_layer.core.artifact_capture import SENSOR_CAPABILITIES, ArtifactCapture
from integration_layer.core.data_delivery import DeliveryRecord, DeliveryService
from integration_layer.core.registry import AdapterRegistry
from integration_layer.core.step_runner import ProgressCallback, StepRunner


class MissionExecutionError(Exception):
    """Defined here, not in orchestration_service.py, to avoid a circular
    import (orchestration_service.py will import this module in a later
    task). orchestration_service.py re-exports this symbol so any
    existing `from integration_layer.api.orchestration_service import
    MissionExecutionError` import keeps working -- that re-export is a
    later task's responsibility, not this one's."""
    pass


@dataclass
class MissionExecutor:
    registry: AdapterRegistry
    audit_log: AuditLog
    analytics: MissionAnalytics
    step_runner: StepRunner = field(default_factory=StepRunner)
    artifact_capture: ArtifactCapture = field(default_factory=ArtifactCapture)
    delivery_service: DeliveryService | None = None
    _mission_outcomes_recorded: set = field(default_factory=set, init=False, repr=False)
    _delivery_records: dict = field(default_factory=dict, init=False, repr=False)

    def resolve_adapter(self, mission: Mission, capability_type) -> RobotAdapter:
        """Step-0-only resolution: honors an explicit mission.assigned_robot_id
        pin if the caller set one before the mission started. Logic moved
        verbatim from OrchestrationService._resolve_adapter."""
        if mission.assigned_robot_id:
            adapter = self.registry.get(mission.assigned_robot_id)
            if adapter is None:
                raise MissionExecutionError(f"assigned robot not registered: {mission.assigned_robot_id}")
            return adapter

        candidates = self.registry.find_capable(capability_type)
        if not candidates:
            raise MissionExecutionError(f"no robot currently advertises {capability_type}")
        return candidates[0]

    def _resolve_adapter_for_capability(self, capability_type) -> RobotAdapter:
        """Used for every step AFTER step 0: pure capability-based
        routing, deliberately ignoring mission.assigned_robot_id -- see
        design gap #2 above for why."""
        candidates = self.registry.find_capable(capability_type)
        if not candidates:
            raise MissionExecutionError(f"no robot currently advertises {capability_type}")
        return candidates[0]

    def start_mission(self, mission: Mission, on_progress: ProgressCallback | None = None) -> MissionStepStatus:
        """Resolves adapter for steps[0] and makes exactly ONE
        execute_mission_step() call -- never through StepRunner. See
        design gap #1 above."""
        if not mission.steps:
            raise MissionExecutionError("mission has no steps")

        first_step = mission.steps[0]
        adapter = self.resolve_adapter(mission, first_step.capability_type)
        mission.assigned_robot_id = adapter.robot_id

        self._record_event(mission.mission_id, adapter.robot_id, "mission_assigned")
        status = adapter.execute_mission_step(mission.mission_id, 0, first_step)
        if on_progress is not None:
            on_progress(status)
        self._record_event(
            mission.mission_id, adapter.robot_id, "step_started", detail=str(first_step.capability_type)
        )
        return status

    def continue_mission(
        self, mission: Mission, from_step_index: int, on_progress: ProgressCallback | None = None
    ) -> MissionStepStatus:
        """Resumes a mission from a step already kicked off (by
        start_mission for from_step_index==0, or by this method's own
        prior advancement for later steps). Polls/retries the current
        step to terminal via step_runner.poll_to_terminal_with_retry()
        (resumable -- the step was already started), then -- while each
        step COMPLETEs and more steps remain -- resolves the next step's
        adapter (possibly a different robot) and drives it via
        step_runner.start() (not yet started). The moment the mission
        reaches ANY terminal outcome, records the post-mission analytics
        outcome -- attributed to mission.assigned_robot_id (the robot
        that OWNS/started the mission), not whichever robot happened to
        execute the last step -- and the 'mission_step_terminal' audit
        event, exactly once per mission_id even across repeated calls."""
        adapter = self.registry.get(mission.assigned_robot_id)
        if adapter is None:
            raise MissionExecutionError(f"assigned robot no longer registered: {mission.assigned_robot_id}")

        step_history: list[MissionStepStatus] = []
        retry_counts_by_module: dict[SystemModule, int] = {}

        step_index = from_step_index
        step = mission.steps[step_index]
        wrapped = self._wrap_progress(mission.mission_id, adapter.robot_id, on_progress, retry_counts_by_module)
        status = self.step_runner.poll_to_terminal_with_retry(
            adapter, mission.mission_id, step_index, step, wrapped
        )
        step_history.append(status)
        self._maybe_capture_artifact(mission.mission_id, step_index, step, status)

        while status.status == MissionStatus.COMPLETED and step_index + 1 < len(mission.steps):
            step_index += 1
            step = mission.steps[step_index]
            adapter = self._resolve_adapter_for_capability(step.capability_type)
            self._record_event(
                mission.mission_id, adapter.robot_id, "step_started", detail=str(step.capability_type)
            )
            wrapped = self._wrap_progress(mission.mission_id, adapter.robot_id, on_progress, retry_counts_by_module)
            status = self.step_runner.start(adapter, mission.mission_id, step_index, step, wrapped)
            step_history.append(status)
            self._maybe_capture_artifact(mission.mission_id, step_index, step, status)

        self._record_event(
            mission.mission_id,
            adapter.robot_id,
            "mission_step_terminal",
            detail=f"status={status.status.value} at step {step_index}",
        )

        if mission.mission_id not in self._mission_outcomes_recorded:
            self._mission_outcomes_recorded.add(mission.mission_id)
            self.analytics.record_mission_outcome(
                mission.assigned_robot_id, status, step_history, retry_counts_by_module
            )
            artifacts = self.artifact_capture.drain(mission.mission_id)
            if self.delivery_service is not None and status.status == MissionStatus.COMPLETED:
                self._delivery_records[mission.mission_id] = self.delivery_service.deliver(
                    mission,
                    status,
                    step_history,
                    artifacts,
                    retry_counts_by_module,
                    self.analytics.get_robot_stats(mission.assigned_robot_id),
                )

        return status

    def _maybe_capture_artifact(
        self, mission_id: str, step_index: int, step: MissionStep, status: MissionStepStatus
    ) -> None:
        """Captures a simulated sensor artifact the instant a sensor-
        capability step reaches COMPLETED -- never for NAVIGATE or any
        other non-terminal/non-success status. No network, no
        dependency on delivery_service being configured at all."""
        if status.status == MissionStatus.COMPLETED and step.capability_type in SENSOR_CAPABILITIES:
            self.artifact_capture.capture(mission_id, step_index, step.capability_type)

    def get_delivery_record(self, mission_id: str) -> DeliveryRecord | None:
        """None for a mission that hasn't reached a terminal outcome
        yet, that reached a non-COMPLETED terminal outcome (delivery is
        never attempted for those), or that was run with no
        delivery_service configured at all."""
        return self._delivery_records.get(mission_id)

    def _wrap_progress(
        self,
        mission_id: str,
        robot_id: str,
        caller_on_progress: ProgressCallback | None,
        retry_tally: dict[SystemModule, int],
    ) -> ProgressCallback:
        """Wraps the caller's on_progress so MissionExecutor can observe
        every status StepRunner emits -- forwarding each unchanged to
        the caller, and additionally detecting StepRunner's synthesized
        retry-attempt markers to tally retries by module and record a
        'step_retrying' audit event.

        Detection is STRUCTURAL, not string-parsing: a retry marker is
        the only case where status.status == IN_PROGRESS AND
        status.failure_reason != FailureReason.NONE. A genuine
        in-progress poll from a real adapter always carries
        failure_reason=NONE (its default) -- only StepRunner's own
        synthesized 'retrying' status pairs IN_PROGRESS with a real,
        non-NONE failure_reason (the reason that triggered the retry).
        This avoids parsing StepRunner's human-readable detail string,
        which is for display, not a machine contract."""

        def _callback(status: MissionStepStatus) -> None:
            if caller_on_progress is not None:
                caller_on_progress(status)
            if status.status == MissionStatus.IN_PROGRESS and status.failure_reason != FailureReason.NONE:
                module = retry_policy.classify_failure(status.failure_reason)
                retry_tally[module] = retry_tally.get(module, 0) + 1
                self._record_event(mission_id, robot_id, "step_retrying", detail=f"module={module.value}")

        return _callback

    def _record_event(self, mission_id: str, robot_id: str, event_type: str, detail: str = "") -> None:
        self.audit_log.record(
            AuditEvent(mission_id=mission_id, robot_id=robot_id, event_type=event_type, detail=detail)
        )
