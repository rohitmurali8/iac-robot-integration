// High-rate command bridge.
//
// The Python adapter decides *what* a robot should do next (per a mission
// step); this node relays *how fast* that translates into low-level
// velocity/joint commands, at a rate Python's interpreter/GC overhead
// can't reliably guarantee. It is a narrow, specific use of C++ -- not a
// general control loop -- and it stays supervised by the Python adapter,
// which sets targets on the topics below rather than commanding the
// robot's control interface directly.
//
// Illustrative: written against the rclcpp API surface for correctness of
// shape, not built/run in this repo -- see README.md. control_rate_hz and
// target_stale_timeout_ms are declared ROS2 parameters (config/params.yaml)
// rather than hard-coded, so the control rate can be tuned per platform
// without a rebuild.

#include <chrono>
#include <memory>

#include "geometry_msgs/msg/twist.hpp"
#include "rclcpp/rclcpp.hpp"

using namespace std::chrono_literals;

namespace iac::integration_layer {

class CommandBridgeNode : public rclcpp::Node {
 public:
  CommandBridgeNode() : rclcpp::Node("command_bridge_node") {
    const auto control_rate_hz = declare_parameter<double>("control_rate_hz", 50.0);
    const auto target_stale_timeout_ms = declare_parameter<int>("target_stale_timeout_ms", 300);
    target_stale_timeout_ = std::chrono::milliseconds(target_stale_timeout_ms);

    // Target set by the Python adapter (low rate, "go roughly this way").
    target_sub_ = create_subscription<geometry_msgs::msg::Twist>(
        "/adapter/velocity_target", 10,
        std::bind(&CommandBridgeNode::on_target, this, std::placeholders::_1));

    // Actual robot control topic (high rate, vendor/platform specific
    // naming -- generic here since this node's job is the relay pattern,
    // not any one OEM's control interface).
    cmd_pub_ = create_publisher<geometry_msgs::msg::Twist>("/cmd_vel", 10);

    // Republish at control-loop rate regardless of how often the adapter
    // updates its target. If the adapter stops publishing, current_target_
    // holds the last value -- staleness is caught by the safety monitor's
    // watchdog, not by this node, keeping this node's only job the relay.
    const auto period = std::chrono::duration<double>(1.0 / control_rate_hz);
    relay_timer_ = create_wall_timer(
        std::chrono::duration_cast<std::chrono::milliseconds>(period),
        std::bind(&CommandBridgeNode::relay_command, this));

    // Must be set here, not left to on_target()'s first call: a
    // default-constructed rclcpp::Time carries a different clock source
    // than now(), and relay_command()'s very first tick (before any
    // target has ever arrived) would otherwise throw
    // "can't subtract times with different time sources" instead of
    // cleanly publishing zero.
    last_target_time_ = now();
  }

 private:
  void on_target(const geometry_msgs::msg::Twist::SharedPtr msg) {
    current_target_ = *msg;
    last_target_time_ = now();
  }

  void relay_command() {
    // If the adapter has never set a target, or it's stale beyond one
    // control period we'd expect a live mission to refresh, publish zero
    // rather than repeat a possibly-stale command indefinitely.
    const auto stale_for = now() - last_target_time_;
    if (stale_for > target_stale_timeout_) {
      cmd_pub_->publish(geometry_msgs::msg::Twist());
      return;
    }
    cmd_pub_->publish(current_target_);
  }

  std::chrono::milliseconds target_stale_timeout_;

  rclcpp::Subscription<geometry_msgs::msg::Twist>::SharedPtr target_sub_;
  rclcpp::Publisher<geometry_msgs::msg::Twist>::SharedPtr cmd_pub_;
  rclcpp::TimerBase::SharedPtr relay_timer_;
  geometry_msgs::msg::Twist current_target_;
  rclcpp::Time last_target_time_;
};

}  // namespace iac::integration_layer

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<iac::integration_layer::CommandBridgeNode>());
  rclcpp::shutdown();
  return 0;
}
