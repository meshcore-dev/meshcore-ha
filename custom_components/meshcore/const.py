"""Constants for the MeshCore integration."""
from enum import IntEnum
from typing import Final

DOMAIN: Final = "meshcore"

# Connection types
CONF_CONNECTION_TYPE: Final = "connection_type"
CONF_USB_PATH: Final = "usb_path"
CONF_BLE_ADDRESS: Final = "ble_address"
CONF_TCP_HOST: Final = "tcp_host"
CONF_TCP_PORT: Final = "tcp_port"
CONF_BAUDRATE: Final = "baudrate"
DEFAULT_BAUDRATE: Final = 115200
DEFAULT_TCP_PORT: Final = 5000
CONF_NAME: Final = "name"
CONF_PUBKEY: Final = "pubkey"

# Connection type options
CONNECTION_TYPE_USB: Final = "usb"
CONNECTION_TYPE_BLE: Final = "ble"
CONNECTION_TYPE_TCP: Final = "tcp"

# Polling settings
CONF_SCAN_INTERVAL: Final = "scan_interval"
DEFAULT_SCAN_INTERVAL: Final = 30  # seconds

# Services
SERVICE_SEND_MESSAGE: Final = "send_message"
SERVICE_SEND_CHANNEL_MESSAGE: Final = "send_channel_message"
SERVICE_EXECUTE_COMMAND: Final = "execute_command"
SERVICE_EXECUTE_COMMAND_UI: Final = "execute_command_ui"
SERVICE_MESSAGE_SCRIPT: Final = "send_ui_message"
ATTR_NODE_ID: Final = "node_id"
ATTR_PUBKEY_PREFIX: Final = "pubkey_prefix"
ATTR_CHANNEL_IDX: Final = "channel_idx"
ATTR_MESSAGE: Final = "message"
ATTR_COMMAND: Final = "command"
ATTR_ENTRY_ID: Final = "entry_id"

# Platform constants
PLATFORM_MESSAGE: Final = "message"

# Entity naming constants
ENTITY_DOMAIN_BINARY_SENSOR: Final = "binary_sensor"
ENTITY_DOMAIN_SENSOR: Final = "sensor"
DEFAULT_DEVICE_NAME: Final = "meshcore"
MESSAGES_SUFFIX: Final = "messages"
CONTACT_SUFFIX: Final = "contact"
CHANNEL_PREFIX: Final = "ch_"

# Repeater subscription constants
CONF_REPEATER_SUBSCRIPTIONS: Final = "repeater_subscriptions"
CONF_REPEATER_NAME: Final = "repeater_name"
CONF_REPEATER_PASSWORD: Final = "repeater_password"
CONF_REPEATER_UPDATE_INTERVAL: Final = "repeater_update_interval"
DEFAULT_REPEATER_UPDATE_INTERVAL: Final = 900  # 15 minutes in seconds
MAX_REPEATER_FAILURES_BEFORE_LOGIN: Final = 3  # After this many failures, try login

# Backoff constants for repeater failures
REPEATER_BACKOFF_BASE: Final = 2  # Base multiplier for exponential backoff
REPEATER_BACKOFF_MAX_MULTIPLIER: Final = 120  # Maximum backoff multiplier (10 minutes when * 5 seconds)

# Generic battery voltage to percentage lookup table
BATTERY_CURVE: Final = [
    (4.20, 100), (4.15, 95), (4.10, 90), (4.05, 85), (4.00, 80),
    (3.95, 75), (3.90, 70), (3.85, 65), (3.80, 60), (3.75, 55),
    (3.70, 50), (3.65, 40), (3.60, 30), (3.55, 20), (3.50, 15),
    (3.45, 10), (3.40, 5), (3.30, 2), (3.20, 0)
]

# Update intervals for different data types
CONF_INFO_INTERVAL: Final = "info_interval"  # For both node info and contacts
CONF_MESSAGES_INTERVAL: Final = "messages_interval"

DEFAULT_UPDATE_TICK: Final = 5   # base polling interval

# Other constants
CONNECTION_TIMEOUT: Final = 10  # seconds

class NodeType(IntEnum):
    CLIENT = 1
    REPEATER = 2
    ROOM_SERVER = 3