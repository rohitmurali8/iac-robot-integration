// Independent safety watchdog.
//
// Runs as its own ROS2 node, separate from the Python adapter process.
// This is the concrete answer to "the Python adapter is not in the
// real-time loop" from DESIGN.md section 1: if the adapter process hangs,
// crashes, or is simply slow under GC pressure, this node must still be
// able to stop the robot. It does not depend on the adapter being alive.
//
// Illustrative: written against the rclcpp API surface for correctness of
// shape, not built/run in this repo (no ROS2 toolchain available here --
// see README.md). A real deployment would tune the proximity threshold
// and watchdog timeout per robot platform, likely via adapter-declared
// config rather than the hard-coded constants below.

#include <chrono>
#include <memory>

#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/laser_scan.hpp"
#include "std_srvs/srv/trigger.hpp"

using namespace std::chrono_literals;

namespace iac::integration_layer {

class SafetyMonitorNode : public rclcpp::Node {
 public:
  SafetyMonitorNode() : rclcpp::Node("safety_monitor_node") {
    proximity_sub_ = create_subscription<sensor_msgs::msg::LaserScan>(
        "/scan", rclcpp::SensorDataQoS(),
        std::bind(&SafetyMonitorNode::on_scan, this, std::placeholders::_1));

    estop_service_ = create_service<std_srvs::srv::Trigger>(
        "/estop",
        std::bind(&SafetyMonitorNode::on_estop_request, this,
                  std::placeholders::_1, std::placeholders::_2));

    // Independent liveness check: if we haven't seen a scan in
    // kScanStaleTimeout, treat sensor loss itself as an unsafe condition
    // rather than silently continuing with stale proximity data.
    watchdog_timer_ = create_wall_timer(
        200ms, std::bind(&SafetyMonitorNode::check_liveness, this));

    last_scan_time_ = now();
    RCLCPP_INFO(get_logger(), "safety_monitor_node ready, min proximity threshold = %.2fm",
                kMinProximityMeters);
  }

 private:
  void on_scan(const sensor_msgs::msg::LaserScan::SharedPtr msg) {
    last_scan_time_ = now();

    for (const float range : msg->ranges) {
      if (range > msg->range_min && range < kMinProximityMeters) {
        trigger_estop("proximity threshold violated");
        return;
      }
    }
  }

  void check_liveness() {
    const auto stale_for = now() - last_scan_time_;
    if (stale_for > kScanStaleTimeout) {
      trigger_estop("proximity sensor data stale");
    }
  }

  void on_estop_request(
      const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
      std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    trigger_estop("manual/adapter-requested stop");
    response->success = true;
    response->message = "estop acknowledged";
  }

  void trigger_estop(const std::string& reason) {
    // In a real deployment this publishes directly to the robot's
    // low-level command interface (vendor-specific, lives in the OEM
    // adapter's domain) or asserts a hardware e-stop line -- kept as a
    // log line here since the point being demonstrated is the independent
    // watchdog pattern, not a specific OEM's stop mechanism.
    RCLCPP_ERROR(get_logger(), "ESTOP TRIGGERED: %s", reason.c_str());
  }

  static constexpr float kMinProximityMeters = 0.3f;
  static constexpr auto kScanStaleTimeout = 500ms;

  rclcpp::Subscription<sensor_msgs::msg::LaserScan>::SharedPtr proximity_sub_;
  rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr estop_service_;
  rclcpp::TimerBase::SharedPtr watchdog_timer_;
  rclcpp::Time last_scan_time_;
};

}  // namespace iac::integration_layer

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<iac::integration_layer::SafetyMonitorNode>());
  rclcpp::shutdown();
  return 0;
}
