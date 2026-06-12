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

## Lovelace Card

A companion Lovelace card is available at [meshcore-card](https://github.com/jpettitt/meshcore-card) for displaying MeshCore node data in your Home Assistant dashboards.

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

## Large Mesh Mode

On dense meshes, creating one Home Assistant entity per discovered contact produces hundreds of low-utility entities and a lot of entity-registry churn. **Large Mesh Mode** (opt-in, off by default) tracks discovered (un-added) contacts as data only — no per-contact entity — while contacts you add to your node keep their entities as usual. Enable it at install time or later in **Configure → Global Settings**.

The discovered-contact dropdown, messaging, services, and the chat panel all keep working; the data-only contacts stay inspectable via an aggregate summary sensor and the `meshcore.get_discovered_contact` service. The only trade-off is no individual connectivity sensor / charting / automation for un-added contacts. See [Contact Management → Large Mesh Mode](https://meshcore-dev.github.io/meshcore-ha/contacts#large-mesh-mode).

## MQTT Upload (Addon/Container Env)

Configuration can be done in the Home Assistant Web UI:

- Settings -> Devices & Services -> MeshCore -> Configure
- MQTT Global Settings
- MQTT Broker Settings (Broker 1-4)

Auth-token mode is easy install by default:

- `meshcore-decoder` is optional.
- If `meshcore-decoder` is not available, the integration automatically falls back to in-process Python signing (`PyNaCl`).
- Signing key is pulled from the connected node via `export_private_key()`.
- If private key export is disabled/blocked on firmware, auth-token upload cannot start.

## Map Auto Uploader (map.meshcore.io)

When enabled (off by default), the integration automatically uploads repeater and room server adverts to [map.meshcore.io](https://map.meshcore.io) when your Companion hears them. A standalone alternative is [map.meshcore.io-uploader](https://github.com/recrof/map.meshcore.io-uploader). Enable in Global Settings if you want Map Auto Uploader.

- Uses the same connection as Home Assistant (USB, BLE, or TCP)
- Requires private key export on firmware (`ENABLE_PRIVATE_KEY_EXPORT=1`)
- Replay protection and signature verification built-in

## Self Diagnostics

When enabled (off by default), the integration polls the locally-attached companion node's own statistics and exposes them as sensor entities — giving the companion the same rich diagnostic tiles a managed repeater has. Enable it in the integration's **Global Settings** (or during initial setup).

- **No mesh traffic.** The polls are local queries to the attached radio (`get_stats_core` / `get_stats_radio` / `get_stats_packets`) — they add no LoRa-mesh traffic and consume no airtime or duty-cycle.
- **Off by default.** No new entities are created until you opt in, so existing installs are unaffected.
- **Poll interval.** Configurable from 60 to 3600 seconds (default 300 s / 5 minutes).
- **Entities created (~14 sensors).** Core: uptime, TX queue length. Radio: noise floor, last RSSI, last SNR, TX airtime, RX airtime. Packets: received, sent, flood/direct TX, flood/direct RX, receive errors. Battery is not duplicated — the companion already exposes battery voltage and percentage.
- **Radio fault flags (3 `problem` binary sensors).** The radio's `errors` field is a bitmask of dispatcher fault events, not a count, so it is decoded into three diagnostic binary sensors with `device_class: problem`:
  - **Packet Pool Exhausted** — the packet buffer pool ran out and a packet was dropped.
  - **CAD Timeout** — Channel Activity Detection stayed busy too long (channel congested, or the radio may be wedged).
  - **RX-Start Timeout** — the radio failed to (re)enter receive mode (possible radio hang).

  Each flag **latches**: the firmware sets it on the first occurrence and clears it only when the radio reboots, so `on` means "this fault has happened at least once since the radio last booted," not "is happening now."

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

- Chat with the community on [Discord](https://discord.com/channels/1495203904898728149/1508972219202535475)
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
