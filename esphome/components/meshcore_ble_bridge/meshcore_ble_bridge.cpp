#include "meshcore_ble_bridge.h"

#ifdef USE_ESP32

#include "esphome/core/log.h"

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <lwip/inet.h>
#include <lwip/sockets.h>
#include <lwip/tcp.h>
#include <unistd.h>

namespace esphome::meshcore_ble_bridge {

static const char *const TAG = "meshcore_ble_bridge";

static const uint8_t NUS_SERVICE_UUID[16] = {
    0x9E, 0xCA, 0xDC, 0x24, 0x5E, 0xE0, 0xA9, 0xE4,
    0x93, 0xF3, 0xA3, 0xB5, 0x01, 0x00, 0x40, 0x6E,
};
static const uint8_t NUS_RX_UUID[16] = {
    0x9E, 0xCA, 0xDC, 0x24, 0x5E, 0xE0, 0xA9, 0xE4,
    0x93, 0xF3, 0xA3, 0xB5, 0x02, 0x00, 0x40, 0x6E,
};
static const uint8_t NUS_TX_UUID[16] = {
    0x9E, 0xCA, 0xDC, 0x24, 0x5E, 0xE0, 0xA9, 0xE4,
    0x93, 0xF3, 0xA3, 0xB5, 0x03, 0x00, 0x40, 0x6E,
};

void MeshCoreBLEBridge::setup() {
  this->setup_complete_ = true;
  this->tcp_rx_buffer_.reserve(MAX_TCP_BUFFER);
  if (!this->start_server_()) {
    ESP_LOGE(TAG, "TCP bridge failed to start on port %u", this->port_);
  }
}

void MeshCoreBLEBridge::dump_config() {
  ESP_LOGCONFIG(TAG, "MeshCore BLE Bridge:");
  ESP_LOGCONFIG(TAG, "  BLE client: %s", this->parent() == nullptr ? "(unset)" : this->parent()->address_str());
  ESP_LOGCONFIG(TAG, "  TCP port: %u", this->port_);
  ESP_LOGCONFIG(TAG, "  TCP no delay: %s", YESNO(this->tcp_no_delay_));
  ESP_LOGCONFIG(TAG, "  Force encrypted BLE link: %s", YESNO(this->force_encryption_));
  ESP_LOGCONFIG(TAG, "  Wait for BLE auth: %s", YESNO(this->wait_for_auth_));
  ESP_LOGCONFIG(TAG, "  Write with response: %s", YESNO(this->write_with_response_));
}

void MeshCoreBLEBridge::loop() {
  if (!this->setup_complete_)
    return;
  if (this->server_fd_ < 0)
    this->start_server_();
  this->accept_client_();
  this->read_tcp_();
}

void MeshCoreBLEBridge::gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                                            esp_ble_gattc_cb_param_t *param) {
  switch (event) {
    case ESP_GATTC_CONNECT_EVT:
      this->reset_ble_state_();
      if (this->force_encryption_) {
        ESP_LOGD(TAG, "Requesting authenticated BLE encryption");
        esp_ble_set_encryption(this->parent()->get_remote_bda(), ESP_BLE_SEC_ENCRYPT_MITM);
      }
      break;

    case ESP_GATTC_DISCONNECT_EVT:
      ESP_LOGW(TAG, "BLE disconnected, closing TCP client");
      this->reset_ble_state_();
      this->close_client_();
      break;

    case ESP_GATTC_SEARCH_CMPL_EVT: {
      auto service_uuid = espbt::ESPBTUUID::from_raw(const_cast<uint8_t *>(NUS_SERVICE_UUID));
      auto rx_uuid = espbt::ESPBTUUID::from_raw(const_cast<uint8_t *>(NUS_RX_UUID));
      auto tx_uuid = espbt::ESPBTUUID::from_raw(const_cast<uint8_t *>(NUS_TX_UUID));

      auto *rx = this->parent()->get_characteristic(service_uuid, rx_uuid);
      auto *tx = this->parent()->get_characteristic(service_uuid, tx_uuid);
      if (rx == nullptr || tx == nullptr) {
        ESP_LOGE(TAG, "MeshCore Nordic UART characteristics not found");
        break;
      }

      this->rx_handle_ = rx->handle;
      this->tx_handle_ = tx->handle;
      auto *cccd = this->parent()->get_config_descriptor(this->tx_handle_);
      this->tx_cccd_handle_ = cccd == nullptr ? 0 : cccd->handle;

      ESP_LOGI(TAG, "BLE handles ready: RX=0x%04X TX=0x%04X CCCD=0x%04X", this->rx_handle_, this->tx_handle_,
               this->tx_cccd_handle_);
      this->maybe_enable_notifications_();
      break;
    }

    case ESP_GATTC_REG_FOR_NOTIFY_EVT:
      if (param->reg_for_notify.handle != this->tx_handle_)
        break;
      this->notify_register_requested_ = false;
      if (param->reg_for_notify.status != ESP_GATT_OK) {
        ESP_LOGE(TAG, "Notify registration failed, status=%d", param->reg_for_notify.status);
        break;
      }
      if (this->tx_cccd_handle_ == 0) {
        ESP_LOGE(TAG, "TX characteristic has no CCCD descriptor; notifications cannot be enabled");
        break;
      } else {
        uint8_t notify_enable[2] = {0x01, 0x00};
        auto err = esp_ble_gattc_write_char_descr(this->parent()->get_gattc_if(), this->parent()->get_conn_id(),
                                                  this->tx_cccd_handle_, sizeof(notify_enable), notify_enable,
                                                  ESP_GATT_WRITE_TYPE_RSP, ESP_GATT_AUTH_REQ_MITM);
        if (err != ESP_GATT_OK)
          ESP_LOGE(TAG, "CCCD write request failed, err=%d", err);
      }
      break;

    case ESP_GATTC_WRITE_DESCR_EVT:
      if (param->write.handle != this->tx_cccd_handle_)
        break;
      if (param->write.status == ESP_GATT_OK) {
        this->mark_ble_ready_();
      } else {
        ESP_LOGE(TAG, "CCCD write failed, status=%d", param->write.status);
      }
      break;

    case ESP_GATTC_WRITE_CHAR_EVT:
      if (param->write.handle == this->rx_handle_ && param->write.status != ESP_GATT_OK) {
        ESP_LOGE(TAG, "BLE write failed, status=%d", param->write.status);
        this->ble_write_in_flight_ = false;
        this->ble_tx_current_.clear();
        this->ble_tx_queue_.clear();
        this->close_client_();
      } else if (param->write.handle == this->rx_handle_) {
        this->ble_write_in_flight_ = false;
        this->ble_tx_current_.clear();
        this->pump_ble_tx_queue_();
      }
      break;

    case ESP_GATTC_NOTIFY_EVT:
      if (param->notify.handle == this->tx_handle_)
        this->send_to_tcp_(param->notify.value, param->notify.value_len);
      break;

    default:
      break;
  }
}

void MeshCoreBLEBridge::gap_event_handler(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t *param) {
  if (event != ESP_GAP_BLE_AUTH_CMPL_EVT)
    return;

  if (!this->address_matches_(param->ble_security.auth_cmpl.bd_addr))
    return;

  if (param->ble_security.auth_cmpl.success) {
    this->auth_complete_ = true;
    ESP_LOGI(TAG, "BLE authentication complete, mode=%d", param->ble_security.auth_cmpl.auth_mode);
    this->maybe_enable_notifications_();
  } else {
    this->auth_complete_ = false;
    ESP_LOGE(TAG, "BLE authentication failed, reason=%d", param->ble_security.auth_cmpl.fail_reason);
    this->close_client_();
  }
}

bool MeshCoreBLEBridge::start_server_() {
  if (this->server_fd_ >= 0)
    return true;

  int fd = ::socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
  if (fd < 0) {
    ESP_LOGE(TAG, "socket() failed, errno=%d", errno);
    return false;
  }

  int reuse = 1;
  ::setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  addr.sin_port = htons(this->port_);

  if (::bind(fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) != 0) {
    ESP_LOGE(TAG, "bind() failed, errno=%d", errno);
    ::close(fd);
    return false;
  }

  if (::listen(fd, 1) != 0) {
    ESP_LOGE(TAG, "listen() failed, errno=%d", errno);
    ::close(fd);
    return false;
  }

  this->set_nonblocking_(fd);
  this->server_fd_ = fd;
  ESP_LOGI(TAG, "MeshCore TCP bridge listening on port %u", this->port_);
  return true;
}

void MeshCoreBLEBridge::accept_client_() {
  if (this->server_fd_ < 0 || this->client_fd_ >= 0)
    return;

  sockaddr_in source_addr{};
  socklen_t addr_len = sizeof(source_addr);
  int fd = ::accept(this->server_fd_, reinterpret_cast<sockaddr *>(&source_addr), &addr_len);
  if (fd < 0) {
    if (errno != EAGAIN && errno != EWOULDBLOCK)
      ESP_LOGW(TAG, "accept() failed, errno=%d", errno);
    return;
  }

  this->set_nonblocking_(fd);
#ifdef TCP_NODELAY
  if (this->tcp_no_delay_) {
    int one = 1;
    ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
  }
#endif

  this->client_fd_ = fd;
  this->tcp_rx_buffer_.clear();
  ESP_LOGI(TAG, "TCP client connected from %s", inet_ntoa(source_addr.sin_addr));

  if (!this->ble_ready_) {
    ESP_LOGW(TAG, "TCP client connected before BLE bridge was ready; closing");
    this->close_client_();
  }
}

void MeshCoreBLEBridge::close_client_() {
  if (this->client_fd_ < 0)
    return;
  ::shutdown(this->client_fd_, SHUT_RDWR);
  ::close(this->client_fd_);
  this->client_fd_ = -1;
  this->tcp_rx_buffer_.clear();
}

void MeshCoreBLEBridge::close_server_() {
  this->close_client_();
  if (this->server_fd_ >= 0) {
    ::close(this->server_fd_);
    this->server_fd_ = -1;
  }
}

void MeshCoreBLEBridge::read_tcp_() {
  if (this->client_fd_ < 0)
    return;

  uint8_t buffer[128];
  for (uint8_t i = 0; i < 4; i++) {
    ssize_t got = ::recv(this->client_fd_, buffer, sizeof(buffer), 0);
    if (got > 0) {
      if (this->tcp_rx_buffer_.size() + static_cast<size_t>(got) > MAX_TCP_BUFFER) {
        ESP_LOGW(TAG, "TCP buffer overflow; closing client");
        this->close_client_();
        return;
      }
      this->tcp_rx_buffer_.insert(this->tcp_rx_buffer_.end(), buffer, buffer + got);
      this->parse_tcp_buffer_();
      continue;
    }

    if (got == 0) {
      ESP_LOGI(TAG, "TCP client disconnected");
      this->close_client_();
      return;
    }

    if (errno == EAGAIN || errno == EWOULDBLOCK)
      return;

    ESP_LOGW(TAG, "recv() failed, errno=%d", errno);
    this->close_client_();
    return;
  }
}

void MeshCoreBLEBridge::parse_tcp_buffer_() {
  while (true) {
    auto start = this->tcp_rx_buffer_.begin();
    while (start != this->tcp_rx_buffer_.end() && *start != 0x3C)
      ++start;
    if (start != this->tcp_rx_buffer_.begin())
      this->tcp_rx_buffer_.erase(this->tcp_rx_buffer_.begin(), start);

    if (this->tcp_rx_buffer_.size() < 3)
      return;

    const size_t len = this->tcp_rx_buffer_[1] | (static_cast<size_t>(this->tcp_rx_buffer_[2]) << 8);
    if (len > MAX_MESHCORE_PAYLOAD) {
      ESP_LOGW(TAG, "Invalid MeshCore TCP payload length %u", static_cast<unsigned>(len));
      this->tcp_rx_buffer_.erase(this->tcp_rx_buffer_.begin());
      continue;
    }

    if (this->tcp_rx_buffer_.size() < len + 3)
      return;

    this->write_ble_(this->tcp_rx_buffer_.data() + 3, len);
    this->tcp_rx_buffer_.erase(this->tcp_rx_buffer_.begin(), this->tcp_rx_buffer_.begin() + len + 3);
  }
}

void MeshCoreBLEBridge::write_ble_(const uint8_t *data, size_t len) {
  if (!this->ble_ready_ || this->rx_handle_ == 0 || len == 0) {
    ESP_LOGW(TAG, "BLE bridge is not ready; dropping TCP frame");
    return;
  }

  for (size_t offset = 0; offset < len; offset += BLE_WRITE_CHUNK) {
    const size_t chunk_len = std::min(BLE_WRITE_CHUNK, len - offset);
    this->ble_tx_queue_.emplace_back(data + offset, data + offset + chunk_len);
  }
  this->pump_ble_tx_queue_();
}

void MeshCoreBLEBridge::pump_ble_tx_queue_() {
  if (!this->ble_ready_ || this->rx_handle_ == 0 || this->ble_write_in_flight_ || this->ble_tx_queue_.empty())
    return;

  this->ble_tx_current_ = this->ble_tx_queue_.front();
  this->ble_tx_queue_.erase(this->ble_tx_queue_.begin());

  auto write_type = this->write_with_response_ ? ESP_GATT_WRITE_TYPE_RSP : ESP_GATT_WRITE_TYPE_NO_RSP;
  this->ble_write_in_flight_ = this->write_with_response_;
  auto err = esp_ble_gattc_write_char(this->parent()->get_gattc_if(), this->parent()->get_conn_id(), this->rx_handle_,
                                      static_cast<uint16_t>(this->ble_tx_current_.size()),
                                      this->ble_tx_current_.data(), write_type, ESP_GATT_AUTH_REQ_MITM);
  if (err != ESP_GATT_OK) {
    ESP_LOGE(TAG, "BLE write request failed, err=%d", err);
    this->ble_write_in_flight_ = false;
    this->ble_tx_current_.clear();
    this->ble_tx_queue_.clear();
    this->close_client_();
  } else if (!this->write_with_response_) {
    this->ble_tx_current_.clear();
    this->pump_ble_tx_queue_();
  }
}

void MeshCoreBLEBridge::send_to_tcp_(const uint8_t *data, size_t len) {
  if (this->client_fd_ < 0 || len > MAX_MESHCORE_PAYLOAD)
    return;

  std::vector<uint8_t> frame;
  frame.reserve(len + 3);
  frame.push_back(0x3E);
  frame.push_back(static_cast<uint8_t>(len & 0xFF));
  frame.push_back(static_cast<uint8_t>((len >> 8) & 0xFF));
  frame.insert(frame.end(), data, data + len);

  if (!this->send_all_(frame.data(), frame.size())) {
    ESP_LOGW(TAG, "TCP send failed; closing client");
    this->close_client_();
  }
}

bool MeshCoreBLEBridge::send_all_(const uint8_t *data, size_t len) {
  size_t sent = 0;
  while (sent < len) {
    ssize_t written = ::send(this->client_fd_, data + sent, len - sent, 0);
    if (written > 0) {
      sent += static_cast<size_t>(written);
      continue;
    }
    if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
      return false;
    return false;
  }
  return true;
}

void MeshCoreBLEBridge::reset_ble_state_() {
  this->auth_complete_ = false;
  this->ble_ready_ = false;
  this->notify_register_requested_ = false;
  this->ble_write_in_flight_ = false;
  this->ble_tx_current_.clear();
  this->ble_tx_queue_.clear();
  this->rx_handle_ = 0;
  this->tx_handle_ = 0;
  this->tx_cccd_handle_ = 0;
}

void MeshCoreBLEBridge::maybe_enable_notifications_() {
  if (this->rx_handle_ == 0 || this->tx_handle_ == 0)
    return;
  if (this->wait_for_auth_ && !this->auth_complete_) {
    ESP_LOGD(TAG, "Waiting for BLE authentication before enabling MeshCore notifications");
    return;
  }
  if (this->notify_register_requested_ || this->ble_ready_)
    return;

  auto err =
      esp_ble_gattc_register_for_notify(this->parent()->get_gattc_if(), this->parent()->get_remote_bda(),
                                        this->tx_handle_);
  if (err != ESP_GATT_OK) {
    ESP_LOGE(TAG, "Notify registration request failed, err=%d", err);
    return;
  }
  this->notify_register_requested_ = true;
}

void MeshCoreBLEBridge::mark_ble_ready_() {
  this->ble_ready_ = true;
  this->node_state = espbt::ClientState::ESTABLISHED;
  ESP_LOGI(TAG, "MeshCore BLE bridge ready");
}

bool MeshCoreBLEBridge::address_matches_(const esp_bd_addr_t address) {
  if (this->parent() == nullptr)
    return false;
  return std::memcmp(address, this->parent()->get_remote_bda(), sizeof(esp_bd_addr_t)) == 0;
}

void MeshCoreBLEBridge::set_nonblocking_(int fd) {
  int flags = ::fcntl(fd, F_GETFL, 0);
  if (flags >= 0)
    ::fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

}  // namespace esphome::meshcore_ble_bridge

#endif  // USE_ESP32
