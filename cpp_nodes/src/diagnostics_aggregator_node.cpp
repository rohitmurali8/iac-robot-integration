// System-health aggregator.
//
// Subscribes to every other subsystem's independent status topic
// (/battery_state from battery_monitor_node, /safety/estop_active from
// safety_monitor_node) and republishes a single diagnostic_msgs/
// DiagnosticArray on /diagnostics -- the standard ROS2 pattern for
// "one place to check overall robot health" (what `ros2 topic echo
// /diagnostics` or a diagnostic_aggregator GUI would consume). This node
// knows nothing about HOW either subsystem decides its own health, only
// how to summarize what they've already published -- the same
// loose-coupling-via-topics principle the rest of this package follows.
//
// Illustrative: written against the rclcpp/diagnostic_msgs API surface
// for correctness of shape, not built/run in this repo -- see README.md.

#include <chrono>
#include <memory>

#include "diagnostic_msgs/msg/diagnostic_array.hpp"
#include "diagnostic_msgs/msg/diagnostic_status.hpp"
#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/battery_state.hpp"
#include "std_msgs/msg/bool.hpp"

using namespace std::chrono_literals;

namespace iac::integration_layer {

class DiagnosticsAggregatorNode : public rclcpp::Node {
 public:
  DiagnosticsAggregatorNode() : rclcpp::Node("diagnostics_aggregator_node") {
    const auto aggregate_rate_hz = declare_parameter<double>("aggregate_rate_hz", 2.0);

    battery_sub_ = create_subscription<sensor_msgs::msg::BatteryState>(
        "/battery_state", 10,
        std::bind(&DiagnosticsAggregatorNode::on_battery, this, std::placeholders::_1));
    estop_sub_ = create_subscription<std_msgs::msg::Bool>(
        "/safety/estop_active", 10,
        std::bind(&DiagnosticsAggregatorNode::on_estop, this, std::placeholders::_1));

    diagnostics_pub_ = create_publisher<diagnostic_msgs::msg::DiagnosticArray>("/diagnostics", 10);

    const auto period = std::chrono::duration<double>(1.0 / aggregate_rate_hz);
    publish_timer_ = create_wall_timer(
        std::chrono::duration_cast<std::chrono::milliseconds>(period),
        std::bind(&DiagnosticsAggregatorNode::publish_diagnostics, this));
  }

 private:
  void on_battery(const sensor_msgs::msg::BatteryState::SharedPtr msg) {
    last_battery_pct_ = msg->percentage * 100.0;
    have_battery_reading_ = true;
  }

  void on_estop(const std_msgs::msg::Bool::SharedPtr msg) { estop_active_ = msg->data; }

  void publish_diagnostics() {
    diagnostic_msgs::msg::DiagnosticArray array;
    array.header.stamp = now();

    array.status.push_back(battery_status());
    array.status.push_back(safety_status());

    diagnostics_pub_->publish(array);
  }

  diagnostic_msgs::msg::DiagnosticStatus battery_status() const {
    diagnostic_msgs::msg::DiagnosticStatus status;
    status.name = "power";
    status.hardware_id = "battery_monitor_node";
    if (!have_battery_reading_) {
      status.level = diagnostic_msgs::msg::DiagnosticStatus::STALE;
      status.message = "no battery reading received yet";
    } else if (last_battery_pct_ <= kCriticalBatteryPct) {
      status.level = diagnostic_msgs::msg::DiagnosticStatus::ERROR;
      status.message = "battery critical";
    } else if (last_battery_pct_ <= kWarnBatteryPct) {
      status.level = diagnostic_msgs::msg::DiagnosticStatus::WARN;
      status.message = "battery low";
    } else {
      status.level = diagnostic_msgs::msg::DiagnosticStatus::OK;
      status.message = "battery nominal";
    }
    return status;
  }

  diagnostic_msgs::msg::DiagnosticStatus safety_status() const {
    diagnostic_msgs::msg::DiagnosticStatus status;
    status.name = "safety";
    status.hardware_id = "safety_monitor_node";
    status.level = estop_active_ ? diagnostic_msgs::msg::DiagnosticStatus::ERROR
                                  : diagnostic_msgs::msg::DiagnosticStatus::OK;
    status.message = estop_active_ ? "estop active" : "nominal";
    return status;
  }

  // Thresholds intentionally mirror battery_monitor_node's own
  // low_battery_warn_pct/critical_battery_pct defaults -- this node
  // reports on the SAME thresholds the power subsystem itself acts on,
  // so "what diagnostics says" and "what the robot actually did" never
  // disagree. A real deployment would source these from the same
  // config/params.yaml entry rather than duplicating the numbers, which
  // is a reasonable future tightening, not something this illustrative
  // node needs to solve.
  static constexpr double kWarnBatteryPct = 20.0;
  static constexpr double kCriticalBatteryPct = 5.0;

  double last_battery_pct_ = 100.0;
  bool have_battery_reading_ = false;
  bool estop_active_ = false;

  rclcpp::Subscription<sensor_msgs::msg::BatteryState>::SharedPtr battery_sub_;
  rclcpp::Subscription<std_msgs::msg::Bool>::SharedPtr estop_sub_;
  rclcpp::Publisher<diagnostic_msgs::msg::DiagnosticArray>::SharedPtr diagnostics_pub_;
  rclcpp::TimerBase::SharedPtr publish_timer_;
};

}  // namespace iac::integration_layer

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<iac::integration_layer::DiagnosticsAggregatorNode>());
  rclcpp::shutdown();
  return 0;
}
