cmake_minimum_required(VERSION 3.8)
project(iac_cpp_nodes)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(std_msgs REQUIRED)
find_package(std_srvs REQUIRED)
find_package(diagnostic_msgs REQUIRED)

add_executable(safety_monitor_node src/safety_monitor_node.cpp)
ament_target_dependencies(safety_monitor_node rclcpp sensor_msgs std_msgs std_srvs)

add_executable(command_bridge_node src/command_bridge_node.cpp)
ament_target_dependencies(command_bridge_node rclcpp geometry_msgs)

add_executable(battery_monitor_node src/battery_monitor_node.cpp)
ament_target_dependencies(battery_monitor_node rclcpp sensor_msgs std_srvs)

add_executable(sensor_payload_node src/sensor_payload_node.cpp)
ament_target_dependencies(sensor_payload_node rclcpp std_msgs std_srvs)

add_executable(diagnostics_aggregator_node src/diagnostics_aggregator_node.cpp)
ament_target_dependencies(diagnostics_aggregator_node rclcpp sensor_msgs std_msgs diagnostic_msgs)

install(TARGETS
  safety_monitor_node
  command_bridge_node
  battery_monitor_node
  sensor_payload_node
  diagnostics_aggregator_node
  DESTINATION lib/${PROJECT_NAME}
)

install(DIRECTORY
  launch
  config
  DESTINATION share/${PROJECT_NAME}
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
