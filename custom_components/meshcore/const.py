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
SERVICE_ADD_SELECTED_CONTACT: Final = "add_selected_contact"
SERVICE_REMOVE_SELECTED_CONTACT: Final = "remove_selected_contact"
SERVICE_CLEANUP_UNAVAILABLE_CONTACTS: Final = "cleanup_unavailable_contacts"

# Select entity placeholders
SELECT_NO_CONTACTS: Final = "Select a contact..."
SELECT_NO_DISCOVERED: Final = "No discovered contacts"
SELECT_NO_ADDED: Final = "No added contacts"
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
CONF_REPEATER_PASSWORD: Final = "password"
CONF_REPEATER_UPDATE_INTERVAL: Final = "update_interval"
CONF_REPEATER_TELEMETRY_ENABLED: Final = "telemetry_enabled"
CONF_REPEATER_DISABLE_PATH_RESET: Final = "disable_path_reset"
DEFAULT_REPEATER_UPDATE_INTERVAL: Final = 7200  # 2 hours in seconds
MIN_UPDATE_INTERVAL: Final = 300  # 5 minutes minimum
MAX_REPEATER_FAILURES_BEFORE_LOGIN: Final = 5  # After this many failures, try login

# Client tracking constants
CONF_TRACKED_CLIENTS: Final = "tracked_clients"
CONF_CLIENT_NAME: Final = "client_name"
CONF_CLIENT_UPDATE_INTERVAL: Final = "update_interval"
CONF_CLIENT_DISABLE_PATH_RESET: Final = "disable_path_reset"
DEFAULT_CLIENT_UPDATE_INTERVAL: Final = 7200  # 2 hours in seconds

# Device monitoring
CONF_DEVICE_DISABLED: Final = "disabled"
AUTO_DISABLE_HOURS: Final = 120  # Auto-disable devices after this many hours without success

# Contact refresh interval
CONF_CONTACT_REFRESH_INTERVAL: Final = "contact_refresh_interval"
DEFAULT_CONTACT_REFRESH_INTERVAL: Final = 60  # 1 minute in seconds

# Self telemetry settings
CONF_SELF_TELEMETRY_ENABLED: Final = "self_telemetry_enabled"
CONF_SELF_TELEMETRY_INTERVAL: Final = "self_telemetry_interval"
DEFAULT_SELF_TELEMETRY_INTERVAL: Final = 300  # 5 minutes in seconds

# Backoff constants for repeater failures
REPEATER_BACKOFF_BASE: Final = 2  # Base multiplier for exponential backoff
REPEATER_BACKOFF_MAX_MULTIPLIER: Final = 120  # Maximum backoff multiplier (10 minutes when * 5 seconds)
MAX_FAILURES_BEFORE_PATH_RESET: Final = 3  # Reset path after this many failures
MAX_RETRY_ATTEMPTS: Final = 5  # Maximum retry attempts within refresh window
MAX_RANDOM_DELAY: Final = 30  # Maximum random delay in seconds


# Generic battery voltage to percentage lookup table
BAT_VMIN: Final = 3000
BAT_VMAX: Final = 4200


# Update intervals for different data types
CONF_INFO_INTERVAL: Final = "info_interval"  # For both node info and contacts
CONF_MESSAGES_INTERVAL: Final = "messages_interval"

DEFAULT_UPDATE_TICK: Final = 5  # base polling interval

# Other constants
CONNECTION_TIMEOUT: Final = 10  # seconds

# Rate limiter settings
RATE_LIMITER_CAPACITY: Final = 20
RATE_LIMITER_REFILL_RATE_SECONDS: Final = 120

# RX_LOG correlation cache settings
RX_LOG_CACHE_MAX_SIZE: Final = 100
RX_LOG_CACHE_TTL_SECONDS: Final = 5.0


class NodeType(IntEnum):
    CLIENT = 1
    REPEATER = 2
    ROOM_SERVER = 3
    SENSOR = 4

