# Actually builds iac_cpp_nodes with a real ROS2 toolchain -- this is
# what closes the "no ROS2 install in this environment" gap the rest of
# this repo's docs are honest about: outside Docker, cpp_nodes/ was
# written for API-shape correctness only and never compiled. Inside
# this container, it is a real colcon build against a real rclcpp.
#
# Build + run from the repo root:
#   docker compose --profile ros2 up --build cpp_nodes
# or directly:
#   docker build -t iac-cpp-nodes -f cpp_nodes/Dockerfile cpp_nodes
#   docker run --rm iac-cpp-nodes

FROM ros:humble-ros-base

SHELL ["/bin/bash", "-c"]

# ros-base includes rclcpp/rclpy and the common message packages
# (sensor_msgs, geometry_msgs, std_msgs, std_srvs) already -- only
# diagnostic_msgs, the build tooling, and the launch packages need
# adding explicitly.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3-colcon-common-extensions \
      build-essential \
      ros-humble-diagnostic-msgs \
      ros-humble-launch \
      ros-humble-launch-ros \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /ros2_ws/src/iac_cpp_nodes
COPY . .

WORKDIR /ros2_ws
RUN source /opt/ros/humble/setup.bash && \
    colcon build --packages-select iac_cpp_nodes --event-handlers console_direct+

# Brings up all 5 nodes together via the package's own launch file.
CMD ["/bin/bash", "-c", "source /opt/ros/humble/setup.bash && source install/setup.bash && ros2 launch iac_cpp_nodes bringup.launch.py"]
