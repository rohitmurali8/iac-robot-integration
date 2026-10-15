// Power subsystem monitor.
//
// Publishes sensor_msgs/BatteryState at a fixed rate and independently
// calls the safety monitor's /estop service once charge crosses the
// critical threshold -- demonstrating cross-node coordination between two
// independent subsystems (power and safety) the same way the Python
// adapters' low-SOC checks independently abort a mission step, without
// either node needing to know the other's internals beyond a published
// topic and a service name.
//
// Illustrative: written against the rclcpp API surface for correctness of
// shape, not built/run in this repo -- see README.md. No real BMS driver
// exists here; battery drain is simulated by a simple per-tick decrement
// so the threshold/service-call logic is demonstrable without hardware.
// A real deployment replaces simulate_drain() with an actual telemetry
// read and leaves the threshold/escalation logic unchanged.

#include <algorithm>
#include <chrono>
#include <memory>

#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/battery_state.hpp"
#include "std_srvs/srv/trigger.hpp"

using namespace std::chrono_literals;

namespace iac::integration_layer {

class BatteryMonitorNode : public rclcpp::Node {
 public:
  BatteryMonitorNode() : rclcpp::Node("battery_monitor_node") {
    const auto publish_rate_hz = declare_parameter<double>("publish_rate_hz", 1.0);
    low_battery_warn_pct_ = declare_parameter<double>("low_battery_warn_pct", 20.0);
    critical_battery_pct_ = declare_parameter<double>("critical_battery_pct", 5.0);

    battery_pub_ = create_publisher<sensor_msgs::msg::BatteryState>("/battery_state", 10);
    estop_client_ = create_client<std_srvs::srv::Trigger>("/estop");

    const auto period = std::chrono::duration<double>(1.0 / publish_rate_hz);
    publish_timer_ = create_wall_timer(
        std::chrono::duration_cast<std::chrono::milliseconds>(period),
        std::bind(&BatteryMonitorNode::tick, this));

    RCLCPP_INFO(get_logger(), "battery_monitor_node ready, critical threshold = %.1f%%",
                critical_battery_pct_);
  }

 private:
  void tick() {
    simulate_drain();

    sensor_msgs::msg::BatteryState msg;
    msg.percentage = static_cast<float>(percent_remaining_ / 100.0);
    msg.power_supply_status = percent_remaining_ > critical_battery_pct_
                                   ? sensor_msgs::msg::BatteryState::POWER_SUPPLY_STATUS_DISCHARGING
                                   : sensor_msgs::msg::BatteryState::POWER_SUPPLY_STATUS_NOT_CHARGING;
    msg.present = true;
    battery_pub_->publish(msg);

    if (percent_remaining_ <= low_battery_warn_pct_ && !warned_) {
      warned_ = true;
      RCLCPP_WARN(get_logger(), "battery at %.1f%%, below low-battery warning threshold",
                  percent_remaining_);
    }

    if (percent_remaining_ <= critical_battery_pct_ && !estop_requested_) {
      estop_requested_ = true;
      request_estop();
    }
  }

  void simulate_drain() {
    // Illustrative only -- see file header. A real node removes this
    // entirely and reads actual telemetry in tick() instead.
    percent_remaining_ = std::max(0.0, percent_remaining_ - kSimulatedDrainPerTickPct);
  }

  void request_estop() {
    if (!estop_client_->service_is_ready()) {
      RCLCPP_ERROR(get_logger(), "battery critical but /estop service is unavailable");
      return;
    }
    auto request = std::make_shared<std_srvs::srv::Trigger::Request>();
    estop_client_->async_send_request(
        request, [this](rclcpp::Client<std_srvs::srv::Trigger>::SharedFuture future) {
          RCLCPP_ERROR(get_logger(), "critical battery (%.1f%%) -- estop requested, ack: %s",
                       percent_remaining_, future.get()->message.c_str());
        });
  }

  static constexpr double kSimulatedDrainPerTickPct = 0.5;

  double percent_remaining_ = 100.0;
  double low_battery_warn_pct_;
  double critical_battery_pct_;
  bool warned_ = false;
  bool estop_requested_ = false;

  rclcpp::Publisher<sensor_msgs::msg::BatteryState>::SharedPtr battery_pub_;
  rclcpp::Client<std_srvs::srv::Trigger>::SharedPtr estop_client_;
  rclcpp::TimerBase::SharedPtr publish_timer_;
};

}  // namespace iac::integration_layer

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<iac::integration_layer::BatteryMonitorNode>());
  rclcpp::shutdown();
  return 0;
}
