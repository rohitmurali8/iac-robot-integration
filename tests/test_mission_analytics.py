"""Tests for MissionAnalytics -- incremental post-mission fleet/robot
performance statistics.

Tested in complete isolation: record_mission_outcome() is called directly
with hand-built MissionStepStatus/dict arguments. No adapter, registry, or
orchestration/executor layer involved -- that wiring is a later task.

step_history contract under test: one MissionStepStatus per distinct
step_index actually reached (terminal status of each step), NOT the full
granular poll/retry telemetry stream. total_steps_executed == len(step_history).
"""

from __future__ import annotations

import pytest

from integration_layer.core.mission import FailureReason, MissionStatus, MissionStepStatus, SystemModule
from integration_layer.core.mission_analytics import MissionAnalytics


def _step_status(
    mission_id: str = "m1",
    step_index: int = 0,
    status: MissionStatus = MissionStatus.COMPLETED,
    failure_reason: FailureReason = FailureReason.NONE,
) -> MissionStepStatus:
    return MissionStepStatus(
        mission_id=mission_id,
        step_index=step_index,
        status=status,
        failure_reason=failure_reason,
    )


class TestRecordMissionOutcome:
    def test_single_successful_mission_updates_robot_and_fleet_stats(self) -> None:
        analytics = MissionAnalytics()
        final_status = _step_status(status=MissionStatus.COMPLETED)
        step_history = [
            _step_status(step_index=0, status=MissionStatus.COMPLETED),
            _step_status(step_index=1, status=MissionStatus.COMPLETED),
        ]

        analytics.record_mission_outcome(
            robot_id="robot-1",
            final_status=final_status,
            step_history=step_history,
            retry_counts_by_module={},
        )

        robot_stats = analytics.get_robot_stats("robot-1")
        assert robot_stats.missions_run == 1
        assert robot_stats.missions_succeeded == 1
        assert robot_stats.missions_failed == 0
        assert robot_stats.missions_paused == 0
        assert robot_stats.total_steps_executed == 2

        fleet_stats = analytics.get_fleet_stats()
        assert fleet_stats.total_missions == 1
        assert fleet_stats.total_succeeded == 1

    def test_failed_mission_increments_failed_counts_and_module_breakdown(self) -> None:
        analytics = MissionAnalytics()
        final_status = _step_status(
            status=MissionStatus.FAILED, failure_reason=FailureReason.CONNECTIVITY_LOST
        )

        analytics.record_mission_outcome(
            robot_id="robot-1",
            final_status=final_status,
            step_history=[final_status],
            retry_counts_by_module={},
        )

        robot_stats = analytics.get_robot_stats("robot-1")
        assert robot_stats.missions_failed == 1

        fleet_stats = analytics.get_fleet_stats()
        assert fleet_stats.total_failed == 1
        assert fleet_stats.failures_by_module[SystemModule.COMMS] == 1

    def test_paused_mission_increments_paused_not_failed(self) -> None:
        analytics = MissionAnalytics()
        final_status = _step_status(
            status=MissionStatus.PAUSED_FOR_INTERVENTION, failure_reason=FailureReason.SENSOR_FAULT
        )

        analytics.record_mission_outcome(
            robot_id="robot-1",
            final_status=final_status,
            step_history=[final_status],
            retry_counts_by_module={},
        )

        robot_stats = analytics.get_robot_stats("robot-1")
        assert robot_stats.missions_paused == 1
        assert robot_stats.missions_failed == 0

        fleet_stats = analytics.get_fleet_stats()
        assert fleet_stats.total_paused == 1
        assert fleet_stats.total_failed == 0
        # Paused missions still contribute to the module failure breakdown,
        # even though they are not counted as "failed".
        assert fleet_stats.failures_by_module[SystemModule.SENSOR] == 1

    def test_aborted_mission_counts_as_failed(self) -> None:
        analytics = MissionAnalytics()
        final_status = _step_status(
            status=MissionStatus.ABORTED, failure_reason=FailureReason.OPERATOR_INTERVENTION_REQUESTED
        )

        analytics.record_mission_outcome(
            robot_id="robot-1",
            final_status=final_status,
            step_history=[final_status],
            retry_counts_by_module={},
        )

        robot_stats = analytics.get_robot_stats("robot-1")
        assert robot_stats.missions_failed == 1

        fleet_stats = analytics.get_fleet_stats()
        assert fleet_stats.total_failed == 1
        assert fleet_stats.failures_by_module[SystemModule.SAFETY] == 1

    def test_retry_counts_accumulate_across_missions(self) -> None:
        analytics = MissionAnalytics()

        analytics.record_mission_outcome(
            robot_id="robot-1",
            final_status=_step_status(status=MissionStatus.COMPLETED),
            step_history=[_step_status(status=MissionStatus.COMPLETED)],
            retry_counts_by_module={SystemModule.COMMS: 2},
        )
        analytics.record_mission_outcome(
            robot_id="robot-1",
            final_status=_step_status(status=MissionStatus.COMPLETED),
            step_history=[_step_status(status=MissionStatus.COMPLETED)],
            retry_counts_by_module={SystemModule.COMMS: 1, SystemModule.SENSOR: 1},
        )

        robot_stats = analytics.get_robot_stats("robot-1")
        assert robot_stats.total_retries == 4
        assert robot_stats.retries_by_module == {SystemModule.COMMS: 3, SystemModule.SENSOR: 1}

    def test_success_rate_and_overall_success_rate_properties(self) -> None:
        analytics = MissionAnalytics()

        for _ in range(2):
            analytics.record_mission_outcome(
                robot_id="robot-1",
                final_status=_step_status(status=MissionStatus.COMPLETED),
                step_history=[_step_status(status=MissionStatus.COMPLETED)],
                retry_counts_by_module={},
            )
        analytics.record_mission_outcome(
            robot_id="robot-1",
            final_status=_step_status(status=MissionStatus.FAILED, failure_reason=FailureReason.TIMEOUT),
            step_history=[_step_status(status=MissionStatus.FAILED, failure_reason=FailureReason.TIMEOUT)],
            retry_counts_by_module={},
        )

        robot_stats = analytics.get_robot_stats("robot-1")
        assert robot_stats.success_rate == pytest.approx(2 / 3)

        fleet_stats = analytics.get_fleet_stats()
        assert fleet_stats.overall_success_rate == pytest.approx(2 / 3)

    def test_success_rate_is_zero_not_division_error_when_no_missions_run(self) -> None:
        analytics = MissionAnalytics()

        robot_stats = analytics.get_robot_stats("never-ran-anything")
        assert robot_stats.success_rate == 0.0

        fleet_stats = analytics.get_fleet_stats()
        assert fleet_stats.overall_success_rate == 0.0

    def test_most_and_least_reliable_robot(self) -> None:
        analytics = MissionAnalytics()

        def _record(robot_id: str, statuses: list[MissionStatus]) -> None:
            for status in statuses:
                failure_reason = FailureReason.NONE if status == MissionStatus.COMPLETED else FailureReason.TIMEOUT
                analytics.record_mission_outcome(
                    robot_id=robot_id,
                    final_status=_step_status(status=status, failure_reason=failure_reason),
                    step_history=[_step_status(status=status, failure_reason=failure_reason)],
                    retry_counts_by_module={},
                )

        _record("A", [MissionStatus.COMPLETED] * 3)
        _record("B", [MissionStatus.COMPLETED] + [MissionStatus.FAILED] * 2)
        _record("C", [MissionStatus.COMPLETED] * 2 + [MissionStatus.FAILED])

        assert analytics.most_reliable_robot() == "A"
        assert analytics.least_reliable_robot() == "B"

    def test_most_reliable_robot_returns_none_when_no_missions_recorded(self) -> None:
        analytics = MissionAnalytics()
        assert analytics.most_reliable_robot() is None
        assert analytics.least_reliable_robot() is None

    def test_get_robot_stats_for_unknown_robot_returns_fresh_empty_stats_not_shared_state(self) -> None:
        analytics = MissionAnalytics()

        first = analytics.get_robot_stats("x")
        first.retries_by_module[SystemModule.COMMS] = 99
        first.missions_run = 42

        second = analytics.get_robot_stats("x")
        assert second.retries_by_module == {}
        assert second.missions_run == 0
