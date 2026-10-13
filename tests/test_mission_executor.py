"""Tests for MissionExecutor -- cross-step mission sequencing and the
post-mission analytics hook.

Sequencing-only tests inject a StubStepRunner (defined below) so a bug in
MissionExecutor's own step-advancement logic can't hide behind, or be
confused with, StepRunner's own retry logic (already independently tested
in Task 4). Routing/attribution tests use real MockRobotAdapter/
CloudFleetAdapter instances in a real AdapterRegistry, since proving
capability-based routing needs real capability advertisement.
"""

from __future__ import annotations

from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.events import AuditLog
from integration_layer.core.mission import (
    FailureReason,
    Mission,
    MissionStatus,
    MissionStep,
    MissionStepStatus,
    SystemModule,
)
from integration_layer.core.mission_analytics import MissionAnalytics
from integration_layer.core.mission_executor import MissionExecutor
from integration_layer.core.registry import AdapterRegistry
from mock_robot.cloud_fleet_adapter import CloudFleetAdapter
from mock_robot.mock_robot_node import MockRobotAdapter


class StubStepRunner:
    """Scripted stand-in for StepRunner, isolating MissionExecutor's
    cross-step sequencing from StepRunner's own (already independently
    tested in Task 4) retry logic. Each call to start()/
    poll_to_terminal_with_retry() consumes one scripted
    (final_status, extra_progress_statuses) pair in order; extra_progress
    statuses are emitted to on_progress BEFORE the final status, letting
    a test simulate StepRunner's real internal emissions (including
    synthetic retry markers)."""

    def __init__(self, scripted_results):
        self._scripted = list(scripted_results)
        self.start_calls = []   # list of (robot_id, step_index)
        self.poll_calls = []    # list of (robot_id, step_index)

    def start(self, adapter, mission_id, step_index, step, on_progress=None):
        self.start_calls.append((adapter.robot_id, step_index))
        return self._consume(on_progress)

    def poll_to_terminal_with_retry(self, adapter, mission_id, step_index, step, on_progress=None):
        self.poll_calls.append((adapter.robot_id, step_index))
        return self._consume(on_progress)

    def _consume(self, on_progress):
        final_status, extra_progress = self._scripted.pop(0)
        if on_progress is not None:
            for s in extra_progress:
                on_progress(s)
            on_progress(final_status)
        return final_status


def _status(mission_id, step_index, status, failure_reason=FailureReason.NONE, detail=""):
    return MissionStepStatus(
        mission_id=mission_id,
        step_index=step_index,
        status=status,
        failure_reason=failure_reason,
        detail=detail,
    )


def _executor_with_stub(stub: StubStepRunner, registry: AdapterRegistry | None = None) -> MissionExecutor:
    return MissionExecutor(
        registry=registry or AdapterRegistry(),
        audit_log=AuditLog(),
        analytics=MissionAnalytics(),
        step_runner=stub,
    )


def test_start_mission_executes_step_zero_exactly_once_without_polling():
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="robo-1", payload={"visual_camera"})
    registry.register(adapter)
    stub = StubStepRunner([])  # empty script -- proves it's never consumed
    executor = _executor_with_stub(stub, registry)

    mission = Mission(
        mission_id="mission-1",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1")],
    )

    status = executor.start_mission(mission)

    assert mission.assigned_robot_id == "robo-1"
    assert status.status in (MissionStatus.IN_PROGRESS, MissionStatus.COMPLETED, MissionStatus.ASSIGNED)
    assert stub.start_calls == []
    assert stub.poll_calls == []


def test_continue_mission_current_step_uses_poll_not_start():
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="robo-1", payload={"visual_camera"})
    registry.register(adapter)
    mission = Mission(
        mission_id="mission-2",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1")],
    )
    completed = _status(mission.mission_id, 0, MissionStatus.COMPLETED)
    stub = StubStepRunner([(completed, [])])
    executor = _executor_with_stub(stub, registry)

    executor.start_mission(mission)
    final_status = executor.continue_mission(mission, 0)

    assert final_status is completed
    assert stub.poll_calls == [("robo-1", 0)]
    assert stub.start_calls == []


def test_continue_mission_advances_to_next_step_via_start():
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="robo-1", payload={"visual_camera"})
    registry.register(adapter)
    mission = Mission(
        mission_id="mission-3",
        steps=[
            MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1"),
            MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-2"),
        ],
    )
    completed_0 = _status(mission.mission_id, 0, MissionStatus.COMPLETED)
    completed_1 = _status(mission.mission_id, 1, MissionStatus.COMPLETED)
    stub = StubStepRunner([(completed_0, []), (completed_1, [])])
    executor = _executor_with_stub(stub, registry)

    executor.start_mission(mission)
    final_status = executor.continue_mission(mission, 0)

    assert final_status is completed_1
    assert stub.poll_calls == [("robo-1", 0)]
    assert stub.start_calls == [("robo-1", 1)]


def test_mission_stops_advancing_on_non_completed_status():
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="robo-1", payload={"visual_camera"})
    registry.register(adapter)
    mission = Mission(
        mission_id="mission-4",
        steps=[
            MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1"),
            MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-2"),
            MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-3"),
        ],
    )
    failed_0 = _status(mission.mission_id, 0, MissionStatus.FAILED, FailureReason.PRECONDITION_FAILED)
    stub = StubStepRunner([(failed_0, [])])
    executor = _executor_with_stub(stub, registry)

    executor.start_mission(mission)
    final_status = executor.continue_mission(mission, 0)

    assert final_status is failed_0
    assert final_status.step_index == 0
    assert stub.start_calls == []


def test_mixed_fleet_multistep_routes_each_step_to_the_capability_matching_robot():
    """The test that would have caught design gap #2: step 1 needs
    DETECT_GAS, which only cloud-01 advertises. If continue_mission's
    advancement loop incorrectly called resolve_adapter() (pin-honoring)
    instead of _resolve_adapter_for_capability() (pure capability
    routing) for step 1, resolve_adapter would see
    mission.assigned_robot_id == "local-01" (set by start_mission for
    step 0) already non-empty and return local-01 again -- so
    stub.start_calls would incorrectly read [("local-01", 1)] instead of
    [("cloud-01", 1)]."""
    registry = AdapterRegistry()
    local = MockRobotAdapter(robot_id="local-01", payload={"visual_camera"})
    cloud = CloudFleetAdapter(robot_id="cloud-01", fleet_manifest={"sensors": ["voc_sensor"]})
    registry.register(local)
    registry.register(cloud)

    mission = Mission(
        mission_id="mission-mixed",
        steps=[
            MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1"),
            MissionStep(capability_type=CapabilityType.DETECT_GAS, target="wp-2"),
        ],
    )
    completed_0 = _status(mission.mission_id, 0, MissionStatus.COMPLETED)
    completed_1 = _status(mission.mission_id, 1, MissionStatus.COMPLETED)
    stub = StubStepRunner([(completed_0, []), (completed_1, [])])
    executor = _executor_with_stub(stub, registry)

    executor.start_mission(mission)
    executor.continue_mission(mission, 0)

    assert mission.assigned_robot_id == "local-01"
    assert stub.start_calls == [("cloud-01", 1)]


def test_on_progress_receives_every_status_in_order():
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="robo-1", payload={"visual_camera"})
    registry.register(adapter)
    mission = Mission(
        mission_id="mission-5",
        steps=[
            MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1"),
            MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-2"),
        ],
    )
    intermediate_1 = _status(mission.mission_id, 0, MissionStatus.IN_PROGRESS)
    intermediate_2 = _status(mission.mission_id, 0, MissionStatus.IN_PROGRESS)
    completed_0 = _status(mission.mission_id, 0, MissionStatus.COMPLETED)
    completed_1 = _status(mission.mission_id, 1, MissionStatus.COMPLETED)
    stub = StubStepRunner(
        [(completed_0, [intermediate_1, intermediate_2]), (completed_1, [])]
    )
    executor = _executor_with_stub(stub, registry)

    collected: list[MissionStepStatus] = []

    kickoff_status = executor.start_mission(mission, on_progress=collected.append)
    executor.continue_mission(mission, 0, on_progress=collected.append)

    assert collected == [kickoff_status, intermediate_1, intermediate_2, completed_0, completed_1]


def test_retry_marker_is_tallied_by_module_and_recorded_as_audit_event():
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="robo-1", payload={"visual_camera"})
    registry.register(adapter)
    audit_log = AuditLog()
    analytics = MissionAnalytics()
    mission = Mission(
        mission_id="mission-6",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1")],
    )
    retry_marker = _status(
        mission.mission_id, 0, MissionStatus.IN_PROGRESS, FailureReason.CONNECTIVITY_LOST
    )
    completed = _status(mission.mission_id, 0, MissionStatus.COMPLETED)
    stub = StubStepRunner([(completed, [retry_marker])])
    executor = MissionExecutor(registry=registry, audit_log=audit_log, analytics=analytics, step_runner=stub)

    executor.start_mission(mission)
    executor.continue_mission(mission, 0)

    events = audit_log.for_mission(mission.mission_id)
    retry_events = [e for e in events if e.event_type == "step_retrying"]
    assert len(retry_events) == 1
    assert retry_events[0].detail == "module=comms"

    stats = analytics.get_robot_stats("robo-1")
    assert stats.retries_by_module == {SystemModule.COMMS: 1}


def test_mission_outcome_recorded_exactly_once_across_repeated_continue_mission_calls():
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="robo-1", payload={"visual_camera"})
    registry.register(adapter)
    analytics = MissionAnalytics()
    mission = Mission(
        mission_id="mission-7",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1")],
    )
    completed_a = _status(mission.mission_id, 0, MissionStatus.COMPLETED)
    completed_b = _status(mission.mission_id, 0, MissionStatus.COMPLETED)
    stub = StubStepRunner([(completed_a, []), (completed_b, [])])
    executor = MissionExecutor(registry=registry, audit_log=AuditLog(), analytics=analytics, step_runner=stub)

    executor.start_mission(mission)
    executor.continue_mission(mission, 0)
    executor.continue_mission(mission, 0)

    assert analytics.get_fleet_stats().total_missions == 1


def test_mission_outcome_attributed_to_assigned_robot_not_last_step_robot():
    registry = AdapterRegistry()
    local = MockRobotAdapter(robot_id="local-01", payload={"visual_camera"})
    cloud = CloudFleetAdapter(robot_id="cloud-01", fleet_manifest={"sensors": ["voc_sensor"]})
    registry.register(local)
    registry.register(cloud)
    analytics = MissionAnalytics()

    mission = Mission(
        mission_id="mission-8",
        steps=[
            MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1"),
            MissionStep(capability_type=CapabilityType.DETECT_GAS, target="wp-2"),
        ],
    )
    completed_0 = _status(mission.mission_id, 0, MissionStatus.COMPLETED)
    completed_1 = _status(mission.mission_id, 1, MissionStatus.COMPLETED)
    stub = StubStepRunner([(completed_0, []), (completed_1, [])])
    executor = MissionExecutor(registry=registry, audit_log=AuditLog(), analytics=analytics, step_runner=stub)

    executor.start_mission(mission)
    executor.continue_mission(mission, 0)

    assert analytics.get_robot_stats("local-01").missions_run == 1
    assert analytics.get_robot_stats("cloud-01").missions_run == 0


def test_resolve_adapter_honors_explicit_pin_for_step_zero():
    registry = AdapterRegistry()
    first = MockRobotAdapter(robot_id="pin-first", payload={"visual_camera"})
    second = MockRobotAdapter(robot_id="pin-second", payload={"visual_camera"})
    registry.register(first)
    registry.register(second)
    stub = StubStepRunner([])
    executor = _executor_with_stub(stub, registry)

    mission = Mission(
        mission_id="mission-9",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1")],
        assigned_robot_id="pin-second",
    )

    executor.start_mission(mission)

    assert mission.assigned_robot_id == "pin-second"
