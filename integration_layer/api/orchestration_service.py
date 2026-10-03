"""The interface orchestration core actually calls.

This is the mission executor: it resolves a Mission to a robot via the
registry, drives each step through the assigned adapter, and reports
status/audit events back. Orchestration core depends on this class's
methods and the schemas in integration_layer.core -- nothing else.

Written as a plain Python service class rather than generated gRPC
server boilerplate, to keep the executor logic readable; schemas/
mission.proto is the canonical wire contract this would sit behind in
a real deployment.
"""

from __future__ import annotations

import time

from integration_layer.core.adapter_base import RobotAdapter
from integration_layer.core.events import AuditEvent, AuditLog
from integration_layer.core.mission import (
    FailureReason,
    Mission,
    MissionStatus,
    MissionStepStatus,
)
from integration_layer.core.registry import AdapterRegistry

_TERMINAL_STATUSES = {MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.ABORTED}


class MissionExecutionError(Exception):
    pass


class OrchestrationService:
    """The one class orchestration core talks to. Everything below this
    (registry, adapters, ROS2/OEM SDK calls) is invisible from here."""

    def __init__(self, registry: AdapterRegistry, audit_log: AuditLog) -> None:
        self._registry = registry
        self._audit_log = audit_log
        # mission_id -> (robot_id, currently executing step index)
        self._mission_progress: dict[str, tuple[str, int]] = {}

    def assign_mission(self, mission: Mission) -> MissionStepStatus:
        """Resolve the mission's first step to a capable robot and begin
        execution. Orchestration core is responsible for having already
        applied scheduling/approval logic -- this method only checks
        physical capability, not business rules."""
        if not mission.steps:
            raise MissionExecutionError("mission has no steps")

        first_step = mission.steps[0]
        adapter = self._resolve_adapter(mission, first_step.capability_type)

        mission.assigned_robot_id = adapter.robot_id
        self._mission_progress[mission.mission_id] = (adapter.robot_id, 0)

        self._record_event(mission.mission_id, adapter.robot_id, "mission_assigned")

        status = adapter.execute_mission_step(mission.mission_id, 0, first_step)
        self._record_event(
            mission.mission_id,
            adapter.robot_id,
            "step_started",
            detail=str(first_step.capability_type),
        )
        return status

    def get_mission_status(self, mission_id: str) -> MissionStepStatus:
        """Poll current status through the assigned adapter. This is the
        one call site that has to work identically whether the underlying
        robot completes a step synchronously (MockRobotAdapter) or only
        resolves after several polls (CloudFleetAdapter) -- callers never
        know which, and never should."""
        progress = self._mission_progress.get(mission_id)
        if progress is None:
            raise MissionExecutionError(f"unknown mission_id: {mission_id}")

        robot_id, step_index = progress
        adapter = self._registry.get(robot_id)
        if adapter is None:
            raise MissionExecutionError(f"assigned robot no longer registered: {robot_id}")

        return adapter.get_status(mission_id, step_index)

    def wait_for_completion(
        self, mission_id: str, max_polls: int = 10, poll_interval_s: float = 0.0
    ) -> MissionStepStatus:
        """Poll get_mission_status() until the step reaches a terminal
        status or max_polls is exhausted. This is the platform-level
        abstraction over sync vs. async OEM behavior: a fast local robot
        resolves on the first poll, a cloud-job-based robot resolves after
        a few -- calling code here is identical either way."""
        status = self.get_mission_status(mission_id)
        polls = 1
        while status.status not in _TERMINAL_STATUSES and polls < max_polls:
            if poll_interval_s:
                time.sleep(poll_interval_s)
            status = self.get_mission_status(mission_id)
            polls += 1

        robot_id, _ = self._mission_progress[mission_id]
        self._record_event(
            mission_id,
            robot_id,
            "mission_step_terminal",
            detail=f"status={status.status.value} after {polls} poll(s)",
        )
        return status

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

    # -- internal ------------------------------------------------------------

    def _resolve_adapter(self, mission: Mission, capability_type) -> RobotAdapter:
        if mission.assigned_robot_id:
            adapter = self._registry.get(mission.assigned_robot_id)
            if adapter is None:
                raise MissionExecutionError(f"assigned robot not registered: {mission.assigned_robot_id}")
            return adapter

        candidates = self._registry.find_capable(capability_type)
        if not candidates:
            raise MissionExecutionError(f"no robot currently advertises {capability_type}")
        # Real scheduling (proximity, battery, current workload) lives in
        # orchestration core; the Integration Layer just picks the first
        # physically-capable candidate here since that logic is explicitly
        # out of scope per DESIGN.md section 2.
        return candidates[0]

    def _record_event(self, mission_id: str, robot_id: str, event_type: str, detail: str = "") -> None:
        self._audit_log.record(
            AuditEvent(mission_id=mission_id, robot_id=robot_id, event_type=event_type, detail=detail)
        )
