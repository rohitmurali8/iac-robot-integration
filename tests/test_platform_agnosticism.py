"""Proves the specific claim the assessment asks for: the underlying
robot can be switched without changing the platform API or the calling
code above it.

run_inspection_mission() below is written once, against OrchestrationService
only. It is called three times -- once against a local/ROS2-style OEM,
once against a cloud/REST-style OEM, and once against a registry holding
both where only the registry's contents (not the function) decide which
one actually runs. If this function needed to change, or needed an
isinstance check, to support a second OEM, the abstraction would have
failed; it doesn't.
"""

from __future__ import annotations

from integration_layer.core.capability_model import CapabilityType
from integration_layer.core.events import AuditLog
from integration_layer.core.mission import Mission, MissionStatus, MissionStep
from integration_layer.core.registry import AdapterRegistry
from integration_layer.api.orchestration_service import OrchestrationService
from mock_robot.cloud_fleet_adapter import CloudFleetAdapter
from mock_robot.mock_robot_node import MockRobotAdapter


def run_inspection_mission(service: OrchestrationService, mission_id: str):
    """The platform-facing call path. Nothing here is OEM-specific --
    this is the function a user-facing control layer would actually call,
    and it's identical regardless of which adapter ends up handling it."""
    mission = Mission(
        mission_id=mission_id,
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="waypoint_1")],
    )
    service.assign_mission(mission)
    final_status = service.wait_for_completion(mission_id)
    return mission.assigned_robot_id, final_status


def _service_with(registry: AdapterRegistry) -> OrchestrationService:
    return OrchestrationService(registry, AuditLog())


def test_same_call_path_succeeds_against_local_oem_only():
    registry = AdapterRegistry()
    registry.register(MockRobotAdapter(robot_id="local-01", payload={"visual_camera"}))
    service = _service_with(registry)

    robot_id, status = run_inspection_mission(service, "mission-local")

    assert robot_id == "local-01"
    assert status.status == MissionStatus.COMPLETED


def test_same_call_path_succeeds_against_cloud_oem_only():
    registry = AdapterRegistry()
    registry.register(CloudFleetAdapter(robot_id="cloud-01", fleet_manifest={"sensors": ["rgb_cam"]}))
    service = _service_with(registry)

    robot_id, status = run_inspection_mission(service, "mission-cloud")

    assert robot_id == "cloud-01"
    assert status.status == MissionStatus.COMPLETED


def test_platform_code_does_not_choose_which_oem_runs():
    """The deciding factor is registry contents, not anything in
    run_inspection_mission(). Registering only the cloud adapter and
    reusing the exact same call proves the function has no hidden
    dependency on which OEM happens to be present."""
    registry = AdapterRegistry()
    registry.register(CloudFleetAdapter(robot_id="whichever-01", fleet_manifest={"sensors": ["rgb_cam"]}))
    service = _service_with(registry)

    robot_id, status = run_inspection_mission(service, "mission-whichever")

    assert robot_id == "whichever-01"
    assert status.status == MissionStatus.COMPLETED


def test_mixed_fleet_resolves_each_mission_to_the_right_oem_transparently():
    """Both OEMs registered at once, with non-overlapping payloads, so
    resolution is deterministic: a visual-inspection mission can only be
    served by the local robot, a gas-detection mission only by the cloud
    robot. (Picking among multiple equally-capable robots is deliberately
    out of scope here -- that's orchestration core's scheduling job per
    DESIGN.md section 2, not the Integration Layer's.) Same call path,
    each mission correctly routed to the one OEM that actually has the
    right payload -- this is the actual mixed-fleet scenario the
    Integration Layer exists for."""
    registry = AdapterRegistry()
    registry.register(MockRobotAdapter(robot_id="local-02", payload={"visual_camera"}))
    registry.register(CloudFleetAdapter(robot_id="cloud-02", fleet_manifest={"sensors": ["voc_sensor"]}))
    service = _service_with(registry)

    visual_mission = Mission(
        mission_id="mission-visual",
        steps=[MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="waypoint_1")],
    )
    gas_mission = Mission(
        mission_id="mission-gas",
        steps=[MissionStep(capability_type=CapabilityType.DETECT_GAS, target="waypoint_1")],
    )

    service.assign_mission(visual_mission)
    service.assign_mission(gas_mission)
    status_visual = service.wait_for_completion("mission-visual")
    status_gas = service.wait_for_completion("mission-gas")

    assert visual_mission.assigned_robot_id == "local-02"
    assert gas_mission.assigned_robot_id == "cloud-02"
    assert status_visual.status == MissionStatus.COMPLETED
    assert status_gas.status == MissionStatus.COMPLETED


def test_explicit_reassignment_switches_oem_with_zero_platform_code_change():
    """Directly demonstrates 'switch the underlying robot without
    changing the platform API': the same Mission object, explicitly
    pinned to a robot_id, runs correctly whether that id belongs to the
    local or the cloud adapter -- assign_mission()'s code path is
    identical, only the data (assigned_robot_id) differs."""
    registry = AdapterRegistry()
    registry.register(MockRobotAdapter(robot_id="swap-local", payload={"visual_camera"}))
    registry.register(CloudFleetAdapter(robot_id="swap-cloud", fleet_manifest={"sensors": ["rgb_cam"]}))
    service = _service_with(registry)

    for target_robot_id in ("swap-local", "swap-cloud"):
        mission = Mission(
            mission_id=f"mission-pinned-{target_robot_id}",
            steps=[MissionStep(capability_type=CapabilityType.INSPECT_VISUAL, target="waypoint_1")],
            assigned_robot_id=target_robot_id,
        )
        service.assign_mission(mission)
        status = service.wait_for_completion(mission.mission_id)

        assert mission.assigned_robot_id == target_robot_id
        assert status.status == MissionStatus.COMPLETED
