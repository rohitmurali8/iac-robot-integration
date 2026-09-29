cmake_minimum_required(VERSION 3.8)
project(iac_cpp_nodes)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(std_srvs REQUIRED)

add_executable(safety_monitor_node safety_monitor_node.cpp)
ament_target_dependencies(safety_monitor_node rclcpp sensor_msgs std_srvs)

add_executable(command_bridge_node command_bridge_node.cpp)
ament_target_dependencies(command_bridge_node rclcpp geometry_msgs)

install(TARGETS
  safety_monitor_node
  command_bridge_node
  DESTINATION lib/${PROJECT_NAME}
)

ament_package()
