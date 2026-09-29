"""Adapter for a generic ROS2 mobile robot.

This is the worked example from DESIGN.md section 1. It targets the common
shape of a ROS2-based quadruped or mobile manipulator: a nav2-style
NavigateToPose action server for locomotion, optional sensor topics
depending on payload, and a battery/odom telemetry stream.

One adapter *class* serves any robot matching this shape -- a bare
quadruped and one with a thermal camera bolted on both use this same code,
because get_capabilities() introspects which topics are actually present
on this robot instance rather than hard-coding a capability list.

Requires a ROS2 (rclpy, nav2_msgs) environment to actually run. Written
against the rclpy/action_client API surface for correctness of shape; not
executed in this repo -- see README.md for what has and hasn't been run.
"""

from __future__ import annotations

import time

import rclpy
from action_msgs.msg import GoalStatus
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient
from rclpy.node import Node
from sensor_msgs.msg import BatteryState, Image
from std_srvs.srv import Trigger

from integration_layer.core.adapter_base import RobotAdapter
from integration_layer.core.capability_model import CapabilityDescriptor, CapabilityType
from integration_layer.core.mission import FailureReason, MissionStatus, MissionStep, MissionStepStatus

# Topics/actions this adapter looks for. A robot advertises a capability
# only if the corresponding topic/action is actually present -- this is
# how the same adapter handles heterogeneous payloads without a new class
# per configuration.
_NAV_ACTION = "navigate_to_pose"
_THERMAL_TOPIC = "/thermal/image"
_VISUAL_TOPIC = "/camera/image_raw"
_GAS_TOPIC = "/gas_sensor/reading"
_ESTOP_SERVICE = "/estop"


class Ros2GenericAdapter(RobotAdapter, Node):
    """Implements RobotAdapter for a generic ROS2 robot.

    The C++ safety_monitor_node (cpp_nodes/safety_monitor_node.cpp) runs
    independently of this adapter and owns the actual e-stop trigger path
    at hardware-loop rate; estop() here delegates to it rather than trying
    to stop the robot from within Python's scheduling/GC overhead.
    """

    def __init__(self, robot_id: str) -> None:
        Node.__init__(self, f"integration_adapter_{robot_id}")
        self.robot_id = robot_id

        self._nav_client = ActionClient(self, NavigateToPose, _NAV_ACTION)
        self._estop_client = self.create_client(Trigger, _ESTOP_SERVICE)

        self._battery_level: float | None = None
        self.create_subscription(BatteryState, "/battery_state", self._on_battery, 10)

        # Payload-dependent subscriptions. Presence of a topic (checked via
        # get_topic_names_and_types) is what get_capabilities() reads, not
        # a static config -- if the thermal camera is unplugged, this robot
        # stops advertising inspect_thermal on its next poll.
        self.create_subscription(Image, _THERMAL_TOPIC, self._on_thermal_frame, 1)
        self.create_subscription(Image, _VISUAL_TOPIC, self._on_visual_frame, 1)

        # In-flight step tracking, keyed by (mission_id, step_index).
        self._active_steps: dict[tuple[str, int], MissionStepStatus] = {}

    # -- RobotAdapter interface ------------------------------------------

    def get_capabilities(self) -> list[CapabilityDescriptor]:
        present_topics = {name for name, _ in self.get_topic_names_and_types()}
        capabilities = [
            CapabilityDescriptor(
                capability_type=CapabilityType.NAVIGATE,
                expected_duration_s_range=(5.0, 300.0),
            )
        ]
        if _VISUAL_TOPIC in present_topics:
            capabilities.append(
                CapabilityDescriptor(
                    capability_type=CapabilityType.INSPECT_VISUAL,
                    required_payload="visual_camera",
                    expected_duration_s_range=(10.0, 60.0),
                )
            )
        if _THERMAL_TOPIC in present_topics:
            capabilities.append(
                CapabilityDescriptor(
                    capability_type=CapabilityType.INSPECT_THERMAL,
                    required_payload="thermal_camera",
                    expected_duration_s_range=(10.0, 60.0),
                )
            )
        if _GAS_TOPIC in present_topics:
            capabilities.append(
                CapabilityDescriptor(
                    capability_type=CapabilityType.DETECT_GAS,
                    required_payload="gas_sensor",
                    expected_duration_s_range=(15.0, 120.0),
                )
            )
        return capabilities

    def execute_mission_step(
        self, mission_id: str, step_index: int, step: MissionStep
    ) -> MissionStepStatus:
        key = (mission_id, step_index)

        if step.capability_type == CapabilityType.NAVIGATE or step.capability_type in (
            CapabilityType.INSPECT_VISUAL,
            CapabilityType.INSPECT_THERMAL,
            CapabilityType.DETECT_GAS,
        ):
            # All sensor-inspection capabilities share a shape: navigate to
            # the target, then hold position for the inspection window.
            # This is the "near-free" reuse DESIGN.md section 4 describes --
            # thermal inspection didn't need a new code path, just a new
            # CapabilityType and topic to read from.
            if not self._nav_client.wait_for_server(timeout_sec=2.0):
                return self._status(mission_id, step_index, MissionStatus.FAILED, FailureReason.CONNECTIVITY_LOST)

            goal = NavigateToPose.Goal()
            goal.pose = self._resolve_target_pose(step.target)
            self._nav_client.send_goal_async(goal)

            status = self._status(mission_id, step_index, MissionStatus.IN_PROGRESS)
            self._active_steps[key] = status
            return status

        # manipulate / weld: scoped but not implemented in v0 -- see
        # DESIGN.md section 4 for why these are deferred/supervised-only.
        return self._status(
            mission_id,
            step_index,
            MissionStatus.FAILED,
            FailureReason.PRECONDITION_FAILED,
            detail=f"{step.capability_type} not supported by v0 adapter",
        )

    def get_status(self, mission_id: str, step_index: int) -> MissionStepStatus:
        key = (mission_id, step_index)
        if key in self._active_steps:
            return self._active_steps[key]
        return MissionStepStatus(
            mission_id=mission_id,
            step_index=step_index,
            status=MissionStatus.FAILED,
            failure_reason=FailureReason.ADAPTER_UNRESPONSIVE,
            detail="no record of this step on this adapter",
        )

    def handle_exception(self, mission_id: str, step_index: int, reason: str) -> None:
        # Cancel whatever nav goal is in flight for this step, if any. The
        # action client keeps its own handle internally; a full
        # implementation tracks the goal handle returned by
        # send_goal_async() per (mission_id, step_index) so it can be
        # cancelled precisely instead of cancelling all goals on the client.
        self._nav_client.cancel_all_goals()
        self._active_steps.pop((mission_id, step_index), None)

    def estop(self) -> None:
        # Delegates to the independent C++ safety node's service -- this
        # call must succeed even if this adapter's own event loop is
        # backed up, which is why it's a separate, minimal service call
        # rather than logic embedded in this process.
        if self._estop_client.wait_for_service(timeout_sec=1.0):
            self._estop_client.call_async(Trigger.Request())

    # -- internal helpers --------------------------------------------------

    def _on_battery(self, msg: BatteryState) -> None:
        self._battery_level = msg.percentage

    def _on_thermal_frame(self, msg: Image) -> None:
        pass  # illustrative: real implementation buffers frames for the
              # active inspect_thermal step's mission window

    def _on_visual_frame(self, msg: Image) -> None:
        pass  # same pattern as _on_thermal_frame, for inspect_visual

    def _resolve_target_pose(self, target: str):
        raise NotImplementedError("waypoint -> PoseStamped lookup, site-specific")

    def _status(
        self,
        mission_id: str,
        step_index: int,
        status: MissionStatus,
        failure_reason: FailureReason = FailureReason.NONE,
        detail: str = "",
    ) -> MissionStepStatus:
        return MissionStepStatus(
            mission_id=mission_id,
            step_index=step_index,
            status=status,
            failure_reason=failure_reason,
            detail=detail,
            timestamp_unix_ms=int(time.time() * 1000),
        )
