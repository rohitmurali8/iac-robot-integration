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
// see README.md). Tunables below are declared ROS2 parameters, loaded
// from config/params.yaml via launch/bringup.launch.py, rather than
// hard-coded constants -- the standard ROS2 pattern for per-robot tuning
// without a rebuild.

#include <chrono>
#include <memory>
#include <string>

#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/laser_scan.hpp"
#include "std_msgs/msg/bool.hpp"
#include "std_srvs/srv/trigger.hpp"

using namespace std::chrono_literals;

namespace iac::integration_layer {

class SafetyMonitorNode : public rclcpp::Node {
 public:
  SafetyMonitorNode() : rclcpp::Node("safety_monitor_node") {
    min_proximity_m_ = declare_parameter<double>("min_proximity_m", 0.3);
    const auto scan_stale_timeout_ms = declare_parameter<int>("scan_stale_timeout_ms", 500);
    scan_stale_timeout_ = std::chrono::milliseconds(scan_stale_timeout_ms);

    proximity_sub_ = create_subscription<sensor_msgs::msg::LaserScan>(
        "/scan", rclcpp::SensorDataQoS(),
        std::bind(&SafetyMonitorNode::on_scan, this, std::placeholders::_1));

    estop_service_ = create_service<std_srvs::srv::Trigger>(
        "/estop",
        std::bind(&SafetyMonitorNode::on_estop_request, this,
                  std::placeholders::_1, std::placeholders::_2));

    // Published on every watchdog tick (not just on trigger) so a late
    // subscriber -- e.g. diagnostics_aggregator_node starting after this
    // node -- still gets the current state immediately, not just the
    // next edge.
    estop_status_pub_ = create_publisher<std_msgs::msg::Bool>("/safety/estop_active", 10);

    // Independent liveness check: if we haven't seen a scan in
    // scan_stale_timeout_, treat sensor loss itself as an unsafe condition
    // rather than silently continuing with stale proximity data.
    watchdog_timer_ = create_wall_timer(
        200ms, std::bind(&SafetyMonitorNode::check_liveness, this));

    last_scan_time_ = now();
    RCLCPP_INFO(get_logger(), "safety_monitor_node ready, min proximity threshold = %.2fm",
                min_proximity_m_);
  }

 private:
  void on_scan(const sensor_msgs::msg::LaserScan::SharedPtr msg) {
    last_scan_time_ = now();

    for (const float range : msg->ranges) {
      if (range > msg->range_min && range < min_proximity_m_) {
        trigger_estop("proximity threshold violated");
        return;
      }
    }
    publish_status(false);
  }

  void check_liveness() {
    const auto stale_for = now() - last_scan_time_;
    if (stale_for > scan_stale_timeout_) {
      trigger_estop("proximity sensor data stale");
    } else {
      publish_status(estop_latched_);
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
    estop_latched_ = true;
    RCLCPP_ERROR(get_logger(), "ESTOP TRIGGERED: %s", reason.c_str());
    publish_status(true);
  }

  void publish_status(bool active) {
    std_msgs::msg::Bool msg;
    msg.data = active;
    estop_status_pub_->publish(msg);
  }

  double min_proximity_m_;
  std::chrono::milliseconds scan_stale_timeout_;
  bool estop_latched_ = false;

  rclcpp::Subscription<sensor_msgs::msg::LaserScan>::SharedPtr proximity_sub_;
  rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr estop_service_;
  rclcpp::Publisher<std_msgs::msg::Bool>::SharedPtr estop_status_pub_;
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
