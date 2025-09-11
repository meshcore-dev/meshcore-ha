---
sidebar_position: 1
title: Installation
---

# Getting Started with Meshcore Home Assistant

This guide will help you install and configure the Meshcore integration for Home Assistant.

## Prerequisites

- Home Assistant 2023.8.0 or newer
- Meshcore node with firmware that supports API commands
- Connection method requirements:
  - **USB**: USB port on the Home Assistant host
  - **BLE**: Bluetooth adapter on the Home Assistant host (direct connection only)
  - **TCP**: Network connectivity to your Meshcore device

## Installation Methods

### Method 1: HACS (Recommended)

[![Add Repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=meshcore-dev&repository=meshcore-ha&category=integration)

1. Make sure you have [HACS](https://hacs.xyz/) installed
2. Add this repository as a custom repository in HACS:
   - Go to HACS > Integrations
   - Click on the three dots in the top right corner
   - Select "Custom repositories"
   - Add `https://github.com/meshcore-dev/meshcore-ha`
   - Select "Integration" as the category
3. Click "Install" on the Meshcore integration
4. Restart Home Assistant

### Method 2: Manual Installation

1. Download the latest release from [GitHub](https://github.com/meshcore-dev/meshcore-ha)
2. Copy the `custom_components/meshcore` directory to your Home Assistant `custom_components` directory
3. Restart Home Assistant

## Configuration

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=meshcore)

1. Go to **Settings** → **Devices & Services**
2. Click **+ Add Integration** and search for "Meshcore"
3. Follow the setup wizard to configure your connection type

### Connection Types

#### USB Connection
- Enter the USB port path (e.g., `/dev/ttyUSB0` or `/dev/ttyACM0`)
- Set the baud rate (default: 115200)

#### BLE Connection
- Select your Meshcore device from discovered devices
- Or enter the Bluetooth address manually
- **Note**: BLE pairing over Home Assistant Bluetooth proxy is not currently working

#### TCP Connection
- Enter the hostname or IP address
- Enter the port number (default: varies by device)

### Configuration Options

During setup, you can configure:

- **Contact Refresh Interval** (30-3600 seconds): How often to refresh the mesh network contact list
- **Self Telemetry Enabled**: Whether to collect telemetry from this node
- **Self Telemetry Interval** (60-3600 seconds): How often to collect telemetry data from this node

## Post-Installation Configuration

After initial setup, you can configure additional monitoring through the integration options:

1. Go to the Meshcore integration
2. Click "Configure"
3. Choose from:
   - **Add Repeater Station**: Monitor repeater nodes in your network
   - **Add Tracked Client**: Track specific client devices
   - **Manage Monitored Devices**: Edit or remove configured devices
   - **Global Settings**: Adjust refresh intervals

### Repeater Configuration
- Select repeater from your contacts
- Enter password (if required)
- Enable/disable telemetry collection
- Set update interval (300-3600 seconds)

### Client Tracking
- Select client device from your contacts
- Set update interval (600-7200 seconds)

## Verification

Once configured, you should see:
- Your Meshcore device in the Devices list
- Meshcore entities available for automations
- Real-time status updates from your mesh network
- Contact sensors for each node in your network

## Troubleshooting

### Connection Issues

#### USB Connection
- Verify the device is properly connected and the correct port is selected
- Try a different baud rate if the default doesn't work
- Check permissions for USB device access
- Common port paths:
  - Linux: `/dev/ttyUSB0`, `/dev/ttyACM0`
  - macOS: `/dev/tty.usbserial-*`

#### BLE Connection  
- Ensure Bluetooth is enabled on your Home Assistant host
- Move closer to the device if signal is weak
- **Important**: BLE pairing over Home Assistant Bluetooth proxy is not currently working until Meshcore supports disabling the PIN requirement
- Only direct connections are supported

#### TCP Connection
- Verify hostname/IP and port are correct
- Check for firewall rules blocking the connection
- Ensure the Meshcore device is reachable on the network
- Test connectivity with ping or telnet first

### Integration Not Working

- **Reload the integration**: If you experience issues, reload the integration to reset its state:
  1. Go to Settings → Devices & Services
  2. Find the Meshcore integration
  3. Click the three dots menu
  4. Select "Reload"
- Check the Home Assistant logs for error messages related to Meshcore
- Verify your Meshcore device is working correctly (try using the Meshcore CLI directly)
- Ensure you have the required permissions to access the device (especially for USB)
- Try restarting Home Assistant after installation

### Repeater and Room Server Issues

- If repeaters or room servers aren't appearing, check that your node has correct time synchronization
- Verify the public key used for repeater/room server login is correct
- Try increasing the repeater update interval if connections are unreliable
- For room servers, make sure you've added them as repeaters first to establish the connection
- Check the Home Assistant logs for detailed error messages related to repeater connections
- Reload the integration if repeater connections become stuck

### Common Error Messages

- **"Cannot connect"**: Device is not responding - check physical connection and power
- **"Failed to get node info"**: Communication established but device not responding to commands - may need firmware update
- **"Connection timed out"**: Device took too long to respond - check baud rate for USB or signal strength for BLE
- **"Failed to log in to repeater"**: Incorrect password or repeater not accepting connections

## Next Steps

- [Configure Sensors](./sensors) to monitor your devices
- [Set up Services](./services) for device control
- [Create Automations](./automation) for smart home scenarios