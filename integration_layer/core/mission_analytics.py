"""Post-mission analytics: incrementally-updated fleet/robot performance
statistics.

record_mission_outcome() is intended to be called automatically by
MissionExecutor (a later task) the instant a mission reaches ANY
terminal state (COMPLETED, FAILED, ABORTED, or PAUSED_FOR_INTERVENTION)
-- this IS the "post-mission automation" the platform promises: no
manual trigger, no separate batch job, no periodic recomputation. Stats
are updated in place at the moment a mission ends, so reading current
stats is always an O(1) dict lookup, never a replay of the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from integration_layer.core import retry_policy
from integration_layer.core.mission import FailureReason, MissionStatus, MissionStepStatus, SystemModule


@dataclass
class RobotStats:
    robot_id: str
    missions_run: int = 0
    missions_succeeded: int = 0
    missions_failed: int = 0
    missions_paused: int = 0
    total_retries: int = 0
    retries_by_module: dict[SystemModule, int] = field(default_factory=dict)
    total_steps_executed: int = 0

    @property
    def success_rate(self) -> float:
        if self.missions_run == 0:
            return 0.0
        return self.missions_succeeded / self.missions_run


@dataclass
class FleetStats:
    total_missions: int = 0
    total_succeeded: int = 0
    total_failed: int = 0
    total_paused: int = 0
    failures_by_module: dict[SystemModule, int] = field(default_factory=dict)

    @property
    def overall_success_rate(self) -> float:
        if self.total_missions == 0:
            return 0.0
        return self.total_succeeded / self.total_missions


class MissionAnalytics:
    def __init__(self) -> None:
        self._robot_stats: dict[str, RobotStats] = {}
        self._fleet_stats = FleetStats()

    def record_mission_outcome(
        self,
        robot_id: str,
        final_status: MissionStepStatus,
        step_history: list[MissionStepStatus],
        retry_counts_by_module: dict[SystemModule, int],
    ) -> None:
        """O(1) amortized: updates the one RobotStats entry for robot_id
        and the one shared FleetStats object in place. ABORTED missions
        (e.g. an operator-triggered abort mid-flight) are bucketed as
        "failed" -- there's no dedicated ABORTED counter in either stats
        shape, since the plan only specifies succeeded/failed/paused."""
        stats = self._robot_stats.setdefault(robot_id, RobotStats(robot_id=robot_id))
        stats.missions_run += 1
        stats.total_steps_executed += len(step_history)
        stats.total_retries += sum(retry_counts_by_module.values())
        for module, count in retry_counts_by_module.items():
            stats.retries_by_module[module] = stats.retries_by_module.get(module, 0) + count

        self._fleet_stats.total_missions += 1

        if final_status.status == MissionStatus.COMPLETED:
            stats.missions_succeeded += 1
            self._fleet_stats.total_succeeded += 1
        elif final_status.status == MissionStatus.PAUSED_FOR_INTERVENTION:
            stats.missions_paused += 1
            self._fleet_stats.total_paused += 1
            self._bucket_fleet_failure(final_status.failure_reason)
        else:  # FAILED or ABORTED
            stats.missions_failed += 1
            self._fleet_stats.total_failed += 1
            self._bucket_fleet_failure(final_status.failure_reason)

    def _bucket_fleet_failure(self, failure_reason: FailureReason) -> None:
        if failure_reason == FailureReason.NONE:
            return
        module = retry_policy.classify_failure(failure_reason)
        self._fleet_stats.failures_by_module[module] = self._fleet_stats.failures_by_module.get(module, 0) + 1

    def get_robot_stats(self, robot_id: str) -> RobotStats:
        """Returns a fresh, empty RobotStats (not None, not an exception)
        if this robot has never had a mission recorded -- a benign,
        renderable default for a GUI/analytics consumer."""
        return self._robot_stats.get(robot_id, RobotStats(robot_id=robot_id))

    def get_fleet_stats(self) -> FleetStats:
        return self._fleet_stats

    def most_reliable_robot(self) -> str | None:
        """Highest success_rate among robots that have run at least one
        mission. None if no robot has run any mission yet."""
        candidates = [s for s in self._robot_stats.values() if s.missions_run > 0]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.success_rate).robot_id

    def least_reliable_robot(self) -> str | None:
        candidates = [s for s in self._robot_stats.values() if s.missions_run > 0]
        if not candidates:
            return None
        return min(candidates, key=lambda s: s.success_rate).robot_id
