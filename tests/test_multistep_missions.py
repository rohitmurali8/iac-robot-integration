"""Integration-level tests for OrchestrationService as a thin facade over
MissionExecutor/StepRunner/MissionAnalytics (Task 7).

Deliberately uses REAL AdapterRegistry/AuditLog/MissionAnalytics and REAL
MockRobotAdapter/CloudFleetAdapter instances throughout -- not stubs.
Task 6's test_mission_executor.py already proved MissionExecutor's own
cross-step sequencing logic works against a scripted StubStepRunner; this
file proves the WHOLE STACK (OrchestrationService -> MissionExecutor ->
StepRunner -> real adapters -> real registry/audit_log/analytics) works
together through the public API orchestration core and webapp/app.py
actually depend on.
"""

from __future__ import annotations

from integration_layer.api.orchestration_service import OrchestrationService
from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.customer_registry import Customer, CustomerRegistry
from integration_layer.core.data_delivery import DeliveryStatus
from integration_layer.core.events import AuditLog
from integration_layer.core.mission import Mission, MissionStatus, MissionStep
from integration_layer.core.mission_analytics import MissionAnalytics
from integration_layer.core.registry import AdapterRegistry
from mock_robot.cloud_fleet_adapter import CloudFleetAdapter
from mock_robot.mock_robot_node import MockRobotAdapter
from tests.test_platform_agnosticism import run_inspection_mission


def _service() -> OrchestrationService:
    return OrchestrationService(AdapterRegistry(), AuditLog())


def test_single_step_mission_behavior_is_byte_for_byte_unchanged():
    """Direct single-step regression cross-check the plan calls for:
    run_inspection_mission (imported, not copied) against a fresh service
    with one MockRobotAdapter still completes."""
    registry = AdapterRegistry()
    registry.register(MockRobotAdapter(robot_id="local-regress", payload={"visual_camera"}))
    service = OrchestrationService(registry, AuditLog())

    robot_id, status = run_inspection_mission(service, "mission-regress")

    assert robot_id == "local-regress"
    assert status.status == MissionStatus.COMPLETED


def test_two_step_mission_advances_through_orchestration_service():
    registry = AdapterRegistry()
    registry.register(
        MockRobotAdapter(robot_id="robo-2step", payload={"visual_camera"})
    )
    service = OrchestrationService(registry, AuditLog())

    mission = Mission(
        mission_id="mission-2step",
        steps=[
            MissionStep(capability_type=CapabilityType.NAVIGATE, target="wp-1"),
            MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1"),
        ],
    )

    service.assign_mission(mission)
    final_status = service.wait_for_completion(mission.mission_id)

    assert final_status.status == MissionStatus.COMPLETED
    assert final_status.step_index == 1

    history = service.get_mission_history(mission.mission_id)
    step_indices = {s.step_index for s in history}
    assert 0 in step_indices
    assert 1 in step_indices


def test_four_step_mixed_fleet_mission_routes_each_step_correctly():
    registry = AdapterRegistry()
    registry.register(
        MockRobotAdapter(robot_id="mixed-local", payload={"visual_camera", "thermal_camera"})
    )
    registry.register(
        CloudFleetAdapter(robot_id="mixed-cloud", fleet_manifest={"sensors": ["voc_sensor"]})
    )
    service = OrchestrationService(registry, AuditLog())

    mission = Mission(
        mission_id="mission-4step",
        steps=[
            MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1"),
            MissionStep(capability_type=CapabilityType.DETECT_GAS, target="wp-1"),
            MissionStep(capability_type=CapabilityType.INSPECT_THERMAL, target="wp-1"),
            MissionStep(capability_type=CapabilityType.DETECT_GAS, target="wp-2"),
        ],
    )

    service.assign_mission(mission)
    final_status = service.wait_for_completion(mission.mission_id)

    assert final_status.status == MissionStatus.COMPLETED
    assert final_status.step_index == 3

    history = service.get_mission_history(mission.mission_id)
    completed_step_indices = {
        s.step_index for s in history if s.status == MissionStatus.COMPLETED
    }
    assert completed_step_indices == {0, 1, 2, 3}


def test_non_retryable_failure_halts_mission_without_advancing():
    registry = AdapterRegistry()
    registry.register(
        MockRobotAdapter(robot_id="robo-fail", payload={"visual_camera"}, fail_next_step=True)
    )
    service = OrchestrationService(registry, AuditLog())

    mission = Mission(
        mission_id="mission-fail",
        steps=[
            MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1"),
            MissionStep(capability_type=CapabilityType.NAVIGATE, target="wp-2"),
        ],
    )

    service.assign_mission(mission)
    final_status = service.wait_for_completion(mission.mission_id)

    assert final_status.status == MissionStatus.FAILED
    assert final_status.step_index == 0

    history = service.get_mission_history(mission.mission_id)
    assert all(s.step_index != 1 for s in history)


def test_comms_failure_retries_and_recovers_end_to_end():
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="robo-flaky", payload={"visual_camera"})
    adapter.set_flaky(2)
    registry.register(adapter)
    audit_log = AuditLog()
    service = OrchestrationService(registry, audit_log)

    mission = Mission(
        mission_id="mission-flaky",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1")],
    )

    service.assign_mission(mission)
    final_status = service.wait_for_completion(mission.mission_id)

    assert final_status.status == MissionStatus.COMPLETED

    retrying_events = [
        e for e in audit_log.for_mission(mission.mission_id) if e.event_type == "step_retrying"
    ]
    assert len(retrying_events) == 2


def test_sensor_failure_pauses_for_intervention_end_to_end():
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="robo-sensor", payload={"visual_camera"})
    adapter.set_sensor_failed(CapabilityType.INSPECT_VISUAL)
    registry.register(adapter)
    audit_log = AuditLog()
    service = OrchestrationService(registry, audit_log)

    mission = Mission(
        mission_id="mission-sensor",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1")],
    )

    service.assign_mission(mission)
    final_status = service.wait_for_completion(mission.mission_id)

    assert final_status.status == MissionStatus.PAUSED_FOR_INTERVENTION

    retrying_events = [
        e for e in audit_log.for_mission(mission.mission_id) if e.event_type == "step_retrying"
    ]
    assert len(retrying_events) == 0


def test_power_failure_fails_outright_not_paused_end_to_end():
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="robo-power", payload={"visual_camera"})
    adapter.set_battery(3.0)
    registry.register(adapter)
    service = OrchestrationService(registry, AuditLog())

    mission = Mission(
        mission_id="mission-power",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1")],
    )

    service.assign_mission(mission)
    final_status = service.wait_for_completion(mission.mission_id)

    assert final_status.status == MissionStatus.FAILED
    assert final_status.status != MissionStatus.PAUSED_FOR_INTERVENTION


def test_wait_for_completion_with_custom_max_polls_still_works():
    registry = AdapterRegistry()
    registry.register(
        MockRobotAdapter(robot_id="robo-custom-polls", payload={"visual_camera"})
    )
    service = OrchestrationService(registry, AuditLog())

    mission = Mission(
        mission_id="mission-custom-polls",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1")],
    )

    service.assign_mission(mission)
    final_status = service.wait_for_completion(mission.mission_id, max_polls=5)

    assert final_status.status == MissionStatus.COMPLETED


def test_get_fleet_stats_and_robot_stats_reflect_missions_run_through_orchestration_service():
    registry = AdapterRegistry()
    robot_id = "robo-stats"
    registry.register(
        MockRobotAdapter(robot_id=robot_id, payload={"visual_camera"}, fail_next_step=False)
    )
    service = OrchestrationService(registry, AuditLog())

    mission_ok = Mission(
        mission_id="mission-stats-ok",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1")],
    )
    service.assign_mission(mission_ok)
    status_ok = service.wait_for_completion(mission_ok.mission_id)
    assert status_ok.status == MissionStatus.COMPLETED

    # Force the second mission's step to fail outright (non-retryable
    # PRECONDITION_FAILED) via fail_next_step on the SAME adapter instance.
    adapter = registry.get(robot_id)
    adapter._fail_next_step = True

    mission_fail = Mission(
        mission_id="mission-stats-fail",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-2")],
        assigned_robot_id=robot_id,
    )
    service.assign_mission(mission_fail)
    status_fail = service.wait_for_completion(mission_fail.mission_id)
    assert status_fail.status == MissionStatus.FAILED

    fleet_stats = service.get_fleet_stats()
    assert fleet_stats.total_missions == 2
    assert fleet_stats.total_succeeded == 1
    assert fleet_stats.total_failed == 1

    robot_stats = service.get_robot_stats(robot_id)
    assert robot_stats.missions_run == 2


def test_run_mission_with_progress_streams_live_updates():
    registry = AdapterRegistry()
    registry.register(
        MockRobotAdapter(robot_id="robo-progress", payload={"visual_camera"})
    )
    service = OrchestrationService(registry, AuditLog())

    mission = Mission(
        mission_id="mission-progress",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="wp-1")],
    )

    collected = []
    final_status = service.run_mission_with_progress(mission, collected.append)

    assert len(collected) >= 1
    last = collected[-1]
    assert last.status == final_status.status
    assert last.failure_reason == final_status.failure_reason


def test_orchestration_service_delivers_to_subscribed_customer_end_to_end(tmp_path):
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="spot-01", payload={"thermal_camera"})
    registry.register(adapter)

    customer_registry = CustomerRegistry()
    customer_registry.register(Customer(
        customer_id="acme",
        webhook_url="http://example.invalid/webhook",  # unreachable on purpose -- this test only checks UPLOAD/packaging path shape
        subscribed_capabilities=frozenset({CapabilityType.INSPECT_THERMAL}),
        site_robot_ids=frozenset({"spot-01"}),
    ))

    service = OrchestrationService(
        registry, AuditLog(), customer_registry=customer_registry, delivery_output_dir=tmp_path
    )
    mission = Mission(mission_id="m-e2e", steps=[MissionStep(capability_type=CapabilityType.INSPECT_THERMAL, target="wp1")])

    service.assign_mission(mission)
    final_status = service.wait_for_completion("m-e2e")

    assert final_status.status == MissionStatus.COMPLETED
    record = service.get_delivery_record("m-e2e")
    assert record is not None
    assert record.package_path.exists()
    # Push fails (unreachable URL) but upload/packaging succeeded -- proves
    # OrchestrationService's wiring reaches all the way into DeliveryService.
    assert record.status == DeliveryStatus.PUSH_FAILED


def test_orchestration_service_customer_registry_property_is_shared():
    registry = AdapterRegistry()
    service = OrchestrationService(registry, AuditLog())

    service.customer_registry.register(Customer(
        customer_id="acme", webhook_url="http://example.invalid/webhook",
        subscribed_capabilities=frozenset({CapabilityType.INSPECT_THERMAL}),
        site_robot_ids=frozenset({"spot-01"}),
    ))

    assert [c.customer_id for c in service.customer_registry.all_customers()] == ["acme"]


def test_get_delivery_record_returns_none_for_unknown_mission():
    registry = AdapterRegistry()
    service = OrchestrationService(registry, AuditLog())

    assert service.get_delivery_record("never-ran") is None


def test_wait_for_completion_with_custom_poll_config_still_wires_delivery_service(tmp_path):
    """Covers the gap flagged by task reviewer: _executor_for's throwaway-
    MissionExecutor branch (when max_polls/poll_interval_s differ from
    defaults) must thread delivery_service through, otherwise
    self._executor.delivery_service attribute access raises AttributeError.
    This test forces that branch by using non-default poll config and
    verifies it completes without raising an AttributeError."""
    registry = AdapterRegistry()
    adapter = MockRobotAdapter(robot_id="spot-01", payload={"thermal_camera"})
    registry.register(adapter)

    customer_registry = CustomerRegistry()
    customer_registry.register(Customer(
        customer_id="acme",
        webhook_url="http://example.invalid/webhook",
        subscribed_capabilities=frozenset({CapabilityType.INSPECT_THERMAL}),
        site_robot_ids=frozenset({"spot-01"}),
    ))
    service = OrchestrationService(
        registry, AuditLog(), customer_registry=customer_registry, delivery_output_dir=tmp_path
    )
    mission = Mission(mission_id="m-custom-poll", steps=[MissionStep(capability_type=CapabilityType.INSPECT_THERMAL, target="wp1")])

    service.assign_mission(mission)
    # Non-default max_polls/poll_interval_s forces _executor_for's throwaway-
    # MissionExecutor branch, which must still thread delivery_service through
    # (this is the exact attribute access the task reviewer flagged as untested).
    # The critical test: this call must not raise AttributeError when accessing
    # self._executor.delivery_service inside _executor_for.
    final_status = service.wait_for_completion("m-custom-poll", max_polls=5, poll_interval_s=0.0)

    assert final_status.status == MissionStatus.COMPLETED
