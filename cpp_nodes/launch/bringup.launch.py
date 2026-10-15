"""Brings up every node in this package together, sharing one parameter
file (config/params.yaml). Standard ROS2 launch-file pattern -- illustrative
like the nodes themselves, not run in this repo (no ROS2 toolchain
available here -- see README.md).

Would run via:
    ros2 launch iac_cpp_nodes bringup.launch.py
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node

# Order matters only for readability here -- ROS2 nodes discover each
# other via topic/service names at runtime, not launch order, so
# diagnostics_aggregator_node coming up before battery_monitor_node (for
# example) is fine; it just reports STALE on /battery_state until the
# first reading arrives.
NODE_EXECUTABLES = [
    "safety_monitor_node",
    "command_bridge_node",
    "battery_monitor_node",
    "sensor_payload_node",
    "diagnostics_aggregator_node",
]


def generate_launch_description() -> LaunchDescription:
    params_file = os.path.join(
        get_package_share_directory("iac_cpp_nodes"), "config", "params.yaml"
    )

    return LaunchDescription(
        [
            Node(
                package="iac_cpp_nodes",
                executable=name,
                name=name,
                parameters=[params_file],
                output="screen",
            )
            for name in NODE_EXECUTABLES
        ]
    )
