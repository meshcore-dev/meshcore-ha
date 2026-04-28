#pragma once

#include "esphome/components/ble_client/ble_client.h"
#include "esphome/components/esp32_ble_tracker/esp32_ble_tracker.h"
#include "esphome/core/component.h"

#ifdef USE_ESP32

#include <esp_gap_ble_api.h>
#include <esp_gattc_api.h>

#include <cstddef>
#include <cstdint>
#include <vector>

namespace esphome::meshcore_ble_bridge {

namespace espbt = esphome::esp32_ble_tracker;

class MeshCoreBLEBridge : public Component, public ble_client::BLEClientNode {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  void gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                           esp_ble_gattc_cb_param_t *param) override;
  void gap_event_handler(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t *param) override;

  void set_port(uint16_t port) { this->port_ = port; }
  void set_tcp_no_delay(bool tcp_no_delay) { this->tcp_no_delay_ = tcp_no_delay; }
  void set_force_encryption(bool force_encryption) { this->force_encryption_ = force_encryption; }
  void set_wait_for_auth(bool wait_for_auth) { this->wait_for_auth_ = wait_for_auth; }
  void set_write_with_response(bool write_with_response) { this->write_with_response_ = write_with_response; }

 protected:
  static constexpr size_t MAX_MESHCORE_PAYLOAD = 300;
  static constexpr size_t MAX_TCP_BUFFER = 1024;
  static constexpr size_t BLE_WRITE_CHUNK = 20;

  bool start_server_();
  void accept_client_();
  void close_client_();
  void close_server_();
  void read_tcp_();
  void parse_tcp_buffer_();
  void write_ble_(const uint8_t *data, size_t len);
  void pump_ble_tx_queue_();
  void send_to_tcp_(const uint8_t *data, size_t len);
  bool send_all_(const uint8_t *data, size_t len);

  void reset_ble_state_();
  bool discover_handles_();
  void maybe_enable_notifications_();
  void mark_ble_ready_();
  bool address_matches_(const esp_bd_addr_t address);
  bool uuid_matches_(const esp_bt_uuid_t &actual, const char *expected);
  void set_nonblocking_(int fd);

  uint16_t port_{5000};
  bool tcp_no_delay_{true};
  bool force_encryption_{true};
  bool wait_for_auth_{true};
  bool write_with_response_{true};
  bool setup_complete_{false};

  int server_fd_{-1};
  int client_fd_{-1};
  std::vector<uint8_t> tcp_rx_buffer_;
  std::vector<uint8_t> ble_tx_current_;
  std::vector<std::vector<uint8_t>> ble_tx_queue_;

  bool auth_complete_{false};
  bool ble_ready_{false};
  bool ble_write_in_flight_{false};
  bool notify_register_requested_{false};
  uint16_t rx_handle_{0};
  uint16_t tx_handle_{0};
  uint16_t tx_cccd_handle_{0};
};

}  // namespace esphome::meshcore_ble_bridge

#endif  // USE_ESP32
