![MeshCore Banner](images/meshcore-bg.png)

# MeshCore for Home Assistant

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=meshcore)
[![Add Repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=meshcore-dev&repository=meshcore-ha&category=integration)

This is a custom Home Assistant integration for MeshCore mesh radio nodes. It allows you to monitor and control MeshCore nodes via USB, BLE, or TCP connections.

> :warning: **Work in Progress**: This integration is under active development. BLE connection method hasn't been thoroughly tested yet.

Core integration is powered by [meshcore-py](https://github.com/meshcore-dev/meshcore_py).

---

## 📖 Documentation

### **[➡️ View Full Documentation](https://meshcore-dev.github.io/meshcore-ha/)**

**Everything you need to know:**
- ✅ Complete feature list
- ✅ Configuration guides  
- ✅ Sensor documentation
- ✅ Service descriptions
- ✅ Automation examples
- ✅ Dashboard templates
- ✅ Troubleshooting guides

---

## Installation

### HACS Installation (Recommended)

1. Make sure you have [HACS](https://hacs.xyz/) installed
2. Add this repository as a custom repository in HACS:
   - Go to HACS > Integrations
   - Click on the three dots in the top right corner
   - Select "Custom repositories"
   - Add the URL of this repository
   - Select "Integration" as the category
3. Click "Install" on the MeshCore integration

### Manual Installation

1. Copy the `custom_components/meshcore` directory to your Home Assistant `custom_components` directory
2. Restart Home Assistant

## Quick Start

1. Go to **Settings** > **Devices & Services**
2. Click **+ Add Integration** and search for "MeshCore"
3. Follow the setup wizard to configure your connection

For detailed configuration instructions, see the [documentation](https://meshcore-dev.github.io/meshcore-ha/).

## MQTT Upload (Addon/Container Env)

This fork can publish `meshcore_raw_event` data to MQTT brokers directly from the integration.
Configuration can be done in the Home Assistant Web UI:

- Settings -> Devices & Services -> MeshCore -> Configure
- MQTT Global Settings
- MQTT Broker Settings (Broker 1-4)

Environment variables are still supported as fallback defaults (useful for Home Assistant Add-on containers).

### Broker Variables (up to 4 brokers)

Use `MESHCORE_HA_MQTT1_*`, `MESHCORE_HA_MQTT2_*`, `MESHCORE_HA_MQTT3_*`, `MESHCORE_HA_MQTT4_*`.

- `ENABLED` (`true`/`false`)
- `SERVER` (hostname)
- `PORT` (default `1883`)
- `TRANSPORT` (`tcp` or `websockets`)
- `USE_TLS` (`true`/`false`)
- `TLS_VERIFY` (`true`/`false`)
- `USERNAME`, `PASSWORD` (for username/password auth)
- `USE_AUTH_TOKEN` (`true`/`false`)
- `TOKEN_AUDIENCE` (required for most token-based broker setups)
- `TOPIC_STATUS` (default: `meshcore/{IATA}/{PUBLIC_KEY}/status`)
- `TOPIC_EVENTS` (default: `meshcore/{IATA}/{PUBLIC_KEY}/packets`)
- `IATA` (optional per broker override)

Global variables:

- `MESHCORE_HA_MQTT_IATA` (default `LOC`)
- `MESHCORE_HA_DECODER_CMD` (default `meshcore-decoder`)
- `MESHCORE_HA_PRIVATE_KEY` (64-byte private key in hex, used for auth-token signing)
- `MESHCORE_HA_TOKEN_TTL_SECONDS` (default `3600`)

### Custom MQTT Example

```bash
MESHCORE_HA_MQTT_IATA=SEA
MESHCORE_HA_MQTT1_ENABLED=true
MESHCORE_HA_MQTT1_SERVER=mqtt.example.com
MESHCORE_HA_MQTT1_PORT=1883
MESHCORE_HA_MQTT1_USERNAME=myuser
MESHCORE_HA_MQTT1_PASSWORD=mypass
```

### Let's Mesh Example (Auth Token)

```bash
MESHCORE_HA_MQTT_IATA=SEA
MESHCORE_HA_PRIVATE_KEY=<YOUR_128_HEX_PRIVATE_KEY>

MESHCORE_HA_MQTT1_ENABLED=true
MESHCORE_HA_MQTT1_SERVER=mqtt-us-v1.letsmesh.net
MESHCORE_HA_MQTT1_PORT=443
MESHCORE_HA_MQTT1_TRANSPORT=websockets
MESHCORE_HA_MQTT1_USE_TLS=true
MESHCORE_HA_MQTT1_USE_AUTH_TOKEN=true
MESHCORE_HA_MQTT1_TOKEN_AUDIENCE=mqtt-us-v1.letsmesh.net

MESHCORE_HA_MQTT2_ENABLED=true
MESHCORE_HA_MQTT2_SERVER=mqtt-eu-v1.letsmesh.net
MESHCORE_HA_MQTT2_PORT=443
MESHCORE_HA_MQTT2_TRANSPORT=websockets
MESHCORE_HA_MQTT2_USE_TLS=true
MESHCORE_HA_MQTT2_USE_AUTH_TOKEN=true
MESHCORE_HA_MQTT2_TOKEN_AUDIENCE=mqtt-eu-v1.letsmesh.net
```

Auth-token mode uses `meshcore-decoder auth-token` under the hood, so `meshcore-decoder` must be installed and available in `PATH` (or set `MESHCORE_HA_DECODER_CMD`).

## Development

### Local Development Setup

1. Clone this repository
2. Copy `custom_components/meshcore` to your Home Assistant config directory
3. Restart Home Assistant
4. Add the integration through the UI

### Testing

Run tests with pytest:
```bash
pytest tests/
```

## Support and Development

- Report issues on [GitHub Issues](https://github.com/meshcore-dev/meshcore-ha/issues)
- Contributions are welcome via pull requests
- Documentation contributions are also welcome!

## Requirements

- Home Assistant (version 2023.8.0 or newer)
- MeshCore node with firmware that supports API commands
- For BLE: Bluetooth adapter on the Home Assistant host (direct connection only; proxy connections don't work with PIN pairing)
- For USB: USB port on the Home Assistant host

## License

This project is licensed under the MIT License - see the LICENSE file for details.
