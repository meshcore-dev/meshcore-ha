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