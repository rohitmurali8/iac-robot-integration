"""The interface orchestration core actually calls.

Thin facade: holds the registry/audit_log/analytics and a shared
MissionExecutor, and exposes the pinned public contract orchestration
core already depends on. assign_mission()/wait_for_completion() behave
identically to before this refactor for a single-step mission with a
healthy robot -- same return values, same mission_assigned/step_started
audit events in the same order -- while multi-step execution, live
progress callbacks (run_mission_with_progress), and fleet analytics
(get_robot_stats/get_fleet_stats) become available underneath. All
actual execution mechanics (per-step polling/retry, cross-step
sequencing, post-mission analytics) live in
MissionExecutor/StepRunner/MissionAnalytics -- this class never
implements any of that itself, only wires it together and preserves the
public contract.

One deliberate, scoped exception to "byte-for-byte identical": the
'mission_step_terminal' audit event's detail string changes from
"status=X after N poll(s)" to "status=X at step N" (MissionExecutor's
format, already committed) -- event_type, mission_id, and robot_id are
unchanged, and no test in this codebase asserts on that detail string's
exact content, so this is a non-breaking, deliberate divergence.
"""

from __future__ import annotations

from integration_layer.core.events import AuditEvent, AuditLog
from integration_layer.core.mission import Mission, MissionStepStatus
from integration_layer.core.mission_analytics import FleetStats, MissionAnalytics, RobotStats
from integration_layer.core.mission_executor import MissionExecutionError, MissionExecutor
from integration_layer.core.registry import AdapterRegistry
from integration_layer.core.step_runner import ProgressCallback, StepRunner

__all__ = ["OrchestrationService", "MissionExecutionError"]

_DEFAULT_MAX_POLLS = 10
_DEFAULT_POLL_INTERVAL_S = 0.0


class OrchestrationService:
    """The one class orchestration core talks to. Everything below this
    (registry, adapters, ROS2/OEM SDK calls, retry policy, analytics) is
    invisible from here."""

    def __init__(
        self,
        registry: AdapterRegistry,
        audit_log: AuditLog,
        analytics: MissionAnalytics | None = None,
    ) -> None:
        self._registry = registry
        self._audit_log = audit_log
        self._analytics = analytics if analytics is not None else MissionAnalytics()
        self._executor = MissionExecutor(registry, audit_log, self._analytics)
        self._mission_progress: dict[str, tuple[str, int]] = {}
        self._mission_step_history: dict[str, list[MissionStepStatus]] = {}
        self._missions: dict[str, Mission] = {}

    def assign_mission(self, mission: Mission) -> MissionStepStatus:
        """PINNED: unchanged return value and audit-event sequence from
        before this refactor -- delegates to MissionExecutor.start_mission(),
        which makes exactly one execute_mission_step() call and returns
        promptly, without polling."""
        self._missions[mission.mission_id] = mission
        status = self._executor.start_mission(mission)
        self._mission_progress[mission.mission_id] = (mission.assigned_robot_id, 0)
        self._mission_step_history.setdefault(mission.mission_id, []).append(status)
        return status

    def get_mission_status(self, mission_id: str) -> MissionStepStatus:
        """Poll current status through the assigned adapter directly --
        a lightweight peek, independent of continue_mission()'s
        advancement/retry logic. Unchanged logic from before this
        refactor; now also appends to the per-mission history."""
        progress = self._mission_progress.get(mission_id)
        if progress is None:
            raise MissionExecutionError(f"unknown mission_id: {mission_id}")

        robot_id, step_index = progress
        adapter = self._registry.get(robot_id)
        if adapter is None:
            raise MissionExecutionError(f"assigned robot no longer registered: {robot_id}")

        status = adapter.get_status(mission_id, step_index)
        self._mission_step_history.setdefault(mission_id, []).append(status)
        return status

    def wait_for_completion(
        self,
        mission_id: str,
        max_polls: int = _DEFAULT_MAX_POLLS,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> MissionStepStatus:
        """PINNED: for a single-step mission with a healthy robot,
        resolves on the first poll exactly as before this refactor --
        same return value. For a multi-step mission, or a step that
        fails with a retryable reason, also advances through subsequent
        steps / retries until the whole mission reaches a terminal
        outcome, recording post-mission analytics automatically.
        Delegates to MissionExecutor.continue_mission()."""
        mission = self._missions.get(mission_id)
        if mission is None:
            raise MissionExecutionError(f"unknown mission_id: {mission_id}")

        _, step_index = self._mission_progress[mission_id]
        executor = self._executor_for(max_polls, poll_interval_s)

        def _tracked(status: MissionStepStatus) -> None:
            self._mission_step_history.setdefault(mission_id, []).append(status)

        status = executor.continue_mission(mission, step_index, on_progress=_tracked)
        self._mission_progress[mission_id] = (mission.assigned_robot_id, status.step_index)
        return status

    def run_mission_with_progress(
        self, mission: Mission, on_progress: ProgressCallback
    ) -> MissionStepStatus:
        """NEW: equivalent to assign_mission() + wait_for_completion()
        combined, but streams every intermediate MissionStepStatus to
        on_progress as it happens (every poll, every retry attempt,
        every step transition) -- not just the final result. This is
        the entry point a later task's GUI live/SSE execution path and
        the CLI demo's live printing use."""
        self._missions[mission.mission_id] = mission

        def _tracked(status: MissionStepStatus) -> None:
            self._mission_step_history.setdefault(mission.mission_id, []).append(status)
            on_progress(status)

        self._executor.start_mission(mission, on_progress=_tracked)
        self._mission_progress[mission.mission_id] = (mission.assigned_robot_id, 0)

        status = self._executor.continue_mission(mission, 0, on_progress=_tracked)
        self._mission_progress[mission.mission_id] = (mission.assigned_robot_id, status.step_index)
        return status

    def get_mission_history(self, mission_id: str) -> list[MissionStepStatus]:
        """Full per-step/per-poll/per-retry status history observed by
        this service for a mission, in chronological order. Returns []
        for an unknown mission_id (not an exception) -- callers (the GUI)
        poll this opportunistically."""
        return list(self._mission_step_history.get(mission_id, []))

    def get_robot_stats(self, robot_id: str) -> RobotStats:
        return self._analytics.get_robot_stats(robot_id)

    def get_fleet_stats(self) -> FleetStats:
        return self._analytics.get_fleet_stats()

    def request_operator_intervention(self, mission_id: str, robot_id: str, reason: str) -> None:
        """Called when a human pauses a mission for manual handoff/teleop.
        Recorded into the same audit schema regardless of which tool (in-
        house or a partner ops platform) triggered the intervention."""
        adapter = self._registry.get(robot_id)
        if adapter is None:
            raise MissionExecutionError(f"unknown robot_id: {robot_id}")

        _, step_index = self._mission_progress.get(mission_id, (robot_id, 0))
        adapter.handle_exception(mission_id, step_index, reason)
        self._record_event(mission_id, robot_id, "operator_intervention", detail=reason)

    def emergency_stop(self, robot_id: str, reason: str) -> bool:
        adapter = self._registry.get(robot_id)
        if adapter is None:
            return False
        adapter.estop()
        self._record_event("", robot_id, "estop_triggered", detail=reason)
        return True

    # -- internal --------------------------------------------------------

    def _executor_for(self, max_polls: int, poll_interval_s: float) -> MissionExecutor:
        """Returns this service's shared MissionExecutor for the default
        poll budget (every existing caller does this), or a fresh,
        throwaway one configured with the requested per-call
        max_polls/poll_interval_s otherwise -- avoids mutating shared
        StepRunner state per call. Shares this service's
        registry/audit_log/analytics either way, so audit recording and
        analytics are unaffected; only the throwaway MissionExecutor's
        own mission-outcome dedup tracking starts empty, which only
        matters if the SAME mission_id is driven through both a
        default-config and a custom-config call -- an edge case no
        current caller exercises."""
        if max_polls == _DEFAULT_MAX_POLLS and poll_interval_s == _DEFAULT_POLL_INTERVAL_S:
            return self._executor
        step_runner = StepRunner(max_polls=max_polls, poll_interval_s=poll_interval_s)
        return MissionExecutor(self._registry, self._audit_log, self._analytics, step_runner=step_runner)

    def _record_event(self, mission_id: str, robot_id: str, event_type: str, detail: str = "") -> None:
        self._audit_log.record(
            AuditEvent(mission_id=mission_id, robot_id=robot_id, event_type=event_type, detail=detail)
        )
