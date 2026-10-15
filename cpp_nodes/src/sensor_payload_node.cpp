// Inspection payload subsystem (visual/thermal/gas sensors).
//
// The hardware-side counterpart to what integration_layer/core/
// artifact_capture.py simulates in Python: a mission step that needs
// INSPECT_VISUAL/INSPECT_THERMAL/DETECT_GAS calls one of the services
// below, which represents the actual sensor read a real OEM adapter
// would trigger over its own vendor-specific SDK before handing the
// result back up to the Python layer. Three services, not one
// parameterized one, because each payload sensor is independently
// present-or-absent (mirrors mock_robot_node.py's `payload: set[str]`
// capability-advertisement pattern) and independently callable.
//
// Illustrative: written against the rclcpp API surface for correctness of
// shape, not built/run in this repo -- see README.md. capture_latency_ms
// simulates the real read latency a camera/thermal/gas sensor driver
// would have; there is no actual hardware behind these services here.

#include <chrono>
#include <memory>
#include <string>
#include <thread>

#include "rclcpp/rclcpp.hpp"
#include "std_msgs/msg/string.hpp"
#include "std_srvs/srv/trigger.hpp"

using namespace std::chrono_literals;

namespace iac::integration_layer {

class SensorPayloadNode : public rclcpp::Node {
 public:
  SensorPayloadNode() : rclcpp::Node("sensor_payload_node") {
    capture_latency_ = std::chrono::milliseconds(declare_parameter<int>("capture_latency_ms", 150));
    visual_camera_enabled_ = declare_parameter<bool>("visual_camera_enabled", true);
    thermal_camera_enabled_ = declare_parameter<bool>("thermal_camera_enabled", true);
    gas_sensor_enabled_ = declare_parameter<bool>("gas_sensor_enabled", false);

    last_capture_pub_ = create_publisher<std_msgs::msg::String>("/payload/last_capture", 10);

    visual_service_ = create_service<std_srvs::srv::Trigger>(
        "/payload/capture_visual",
        [this](const std::shared_ptr<std_srvs::srv::Trigger::Request> req,
               std::shared_ptr<std_srvs::srv::Trigger::Response> res) {
          capture("visual", visual_camera_enabled_, req, res);
        });
    thermal_service_ = create_service<std_srvs::srv::Trigger>(
        "/payload/capture_thermal",
        [this](const std::shared_ptr<std_srvs::srv::Trigger::Request> req,
               std::shared_ptr<std_srvs::srv::Trigger::Response> res) {
          capture("thermal", thermal_camera_enabled_, req, res);
        });
    gas_service_ = create_service<std_srvs::srv::Trigger>(
        "/payload/capture_gas",
        [this](const std::shared_ptr<std_srvs::srv::Trigger::Request> req,
               std::shared_ptr<std_srvs::srv::Trigger::Response> res) {
          capture("gas", gas_sensor_enabled_, req, res);
        });

    RCLCPP_INFO(get_logger(), "sensor_payload_node ready (visual=%d thermal=%d gas=%d)",
                visual_camera_enabled_, thermal_camera_enabled_, gas_sensor_enabled_);
  }

 private:
  void capture(const std::string& sensor_name, bool enabled,
               const std::shared_ptr<std_srvs::srv::Trigger::Request>& /*request*/,
               const std::shared_ptr<std_srvs::srv::Trigger::Response>& response) {
    if (!enabled) {
      // Mirrors mock_robot_node.py's capability-advertisement model: a
      // sensor not in the robot's current payload simply isn't callable,
      // the same way it wouldn't appear in get_capabilities().
      response->success = false;
      response->message = sensor_name + " sensor not present in current payload";
      return;
    }

    // Simulated hardware read latency -- see file header. A blocking
    // sleep here is intentional and matches real single-threaded sensor
    // driver behavior (this service call blocks the caller for the
    // duration of the "read"), not an oversight.
    std::this_thread::sleep_for(capture_latency_);

    response->success = true;
    response->message = sensor_name + " capture complete";

    std_msgs::msg::String summary;
    summary.data = sensor_name + " capture at t=" + std::to_string(now().seconds());
    last_capture_pub_->publish(summary);
  }

  std::chrono::milliseconds capture_latency_;
  bool visual_camera_enabled_;
  bool thermal_camera_enabled_;
  bool gas_sensor_enabled_;

  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr last_capture_pub_;
  rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr visual_service_;
  rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr thermal_service_;
  rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr gas_service_;
};

}  // namespace iac::integration_layer

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<iac::integration_layer::SensorPayloadNode>());
  rclcpp::shutdown();
  return 0;
}
