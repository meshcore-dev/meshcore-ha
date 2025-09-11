---
sidebar_position: 7
title: Remote Node Tracking
---

# Remote Node Tracking

The Meshcore Home Assistant integration can monitor and collect data from remote nodes in your mesh network, including Repeaters, Room Servers, and Client devices.

## Overview

Remote node tracking allows you to:
- Monitor repeater statistics and performance metrics
- Collect telemetry from client devices (sensors, GPS, etc.)
- Track battery levels and uptime across the network
- Receive real-time updates based on configured intervals

## Node Types

### Repeaters
Full-featured mesh nodes that relay messages and provide detailed statistics.

**Features:**
- Login authentication support
- Comprehensive statistics (messages, airtime, queue status)
- Telemetry collection capability
- Automatic reconnection on failure

### Room Servers
Full mesh nodes with store-and-forward message capabilities.

**Features:**
- Store and forward message handling
- Login authentication support
- Fewer statistics than repeaters
- No telemetry collection

### Clients
End devices that primarily send telemetry data.

**Features:**
- Telemetry data collection only
- No login required (but may have ACL restrictions)
- Battery monitoring
- Sensor data (temperature, humidity, GPS, etc.)

## Configuration

### Adding a Repeater

1. Navigate to **Settings → Devices & Services**
2. Find your Meshcore integration
3. Click **Configure**
4. Select **Add Repeater Station**
5. Choose the repeater from your contacts list
6. Configure:
   - **Password**: Required if the repeater has authentication
   - **Enable Telemetry**: Collect sensor data from the repeater
   - **Update Interval**: 300-3600 seconds (default: 900)
7. Click **Submit**

The integration will:
- Attempt to log into the repeater
- Verify connectivity
- Retrieve firmware version
- Begin collecting statistics

### Adding a Client

1. Navigate to **Settings → Devices & Services**
2. Find your Meshcore integration
3. Click **Configure**
4. Select **Add Tracked Client**
5. Choose the client from your contacts list
6. Configure:
   - **Update Interval**: 600-7200 seconds (default: 1800)
7. Click **Submit**

The integration will:
- Send telemetry requests at the configured interval
- Create sensors for received data
- Monitor connection status

## Update Intervals

### Recommended Settings

**Repeaters:**
- High activity networks: 300-600 seconds
- Normal networks: 900-1800 seconds
- Low activity/battery conscious: 1800-3600 seconds

**Clients:**
- Critical sensors: 600-1200 seconds
- Standard monitoring: 1800-3600 seconds
- Battery-powered devices: 3600-7200 seconds

### Interval Considerations

- Shorter intervals provide more real-time data but increase network traffic
- Battery-powered nodes benefit from longer intervals
- Network congestion may require longer intervals
- Failed updates trigger exponential backoff

## Authentication & Permissions

### Repeater Authentication

Repeaters typically require a password for login:
1. The integration sends a login command with the password
2. On success, a session is established
3. The session persists until the repeater/room server reboots or evicts the session due to limited storage
4. Automatic re-login occurs after failures

### Client Permissions

Clients may have Access Control Lists (ACLs) that restrict:
- Who can request telemetry
- What data is shared
- Update frequency limits

If a client doesn't respond to telemetry requests:
- Check if your node is authorized in the client's ACL
- Verify the client is within radio range
- Ensure the client has telemetry enabled

## Failure Handling

### Exponential Backoff

When updates fail, the integration implements exponential backoff:

1. **First Failure**: Retry at next scheduled interval
2. **Consecutive Failures**: Double the delay each time
3. **Maximum Backoff**: Caps at ~17 minutes
4. **Recovery**: Resets to normal interval on success

### Automatic Re-login

For repeaters, if status updates fail:
1. Integration attempts to re-login automatically
2. Uses stored password from configuration
3. Resumes normal updates on successful login
4. Applies backoff if login fails repeatedly

## Data Collection

### Repeater Statistics

Updated at each interval:
- Battery voltage and percentage
- Uptime (minutes/days)
- Message counters (sent, received, direct, flood)
- Airtime utilization
- Queue status
- Duplicate message filtering stats
- Noise floor measurements

See [Sensors documentation](./sensors.md#repeater-sensors) for complete list.

### Client Telemetry

Collected when available:
- Environmental sensors (temperature, humidity, light)
- Electrical measurements (voltage, current)
- Motion/presence detection
- GPS location
- Battery status
- Custom Cayenne LPP data

See [Sensors documentation](./sensors.md#telemetry-sensors-cayenne-lpp) for supported types.

## Managing Tracked Nodes

### View Current Configuration

1. Go to **Settings → Devices & Services**
2. Click **Configure** on Meshcore integration
3. Select **Manage Monitored Devices**
4. View list of tracked repeaters and clients

### Edit Node Settings

1. In **Manage Monitored Devices**
2. Select the node to edit
3. Choose **Edit**
4. Modify settings:
   - Update interval
   - Password (repeaters)
   - Telemetry collection (repeaters)
5. Click **Submit**

### Remove Tracked Node

1. In **Manage Monitored Devices**
2. Select the node to remove
3. Choose **Remove**
4. Confirm removal

## Performance Optimization

### Network Traffic

Each update cycle generates:
- **Repeater Status**: 1 request + 1 response
- **Repeater Telemetry**: 1 additional request + response
- **Client Telemetry**: 1 request + possible response

### Best Practices

Make the update interval as high as you can to support your needs to avoid excess mesh traffic.

### Troubleshooting High Failure Rates

If nodes frequently fail to update:
1. Check radio signal strength (RSSI/SNR)
2. Verify node is powered and online
3. Increase update interval
4. Check for network congestion
5. Review repeater passwords
6. Verify client ACL permissions
7. Set a direct path to the remote node via the `update_contact` command if you have a stable path

## Entity Organization

Tracked nodes create organized entity structures:

### Repeater Entities
- Device: `Meshcore Repeater - [Name]`
- Sensors: All statistics under this device
- Telemetry: If enabled, appears under same device

### Client Entities
- Device: `Meshcore Client - [Name]`
- Sensors: All telemetry under this device
- GPS: Creates device_tracker if GPS data received

## Automation Examples

### Low Battery Alert
```yaml
alias: Repeater Low Battery
trigger:
  - platform: numeric_state
    entity_id: sensor.meshcore_abc123_repeater1_battery_percentage
    below: 20
action:
  - service: notify.notify
    data:
      title: "Repeater Battery Low"
      message: "{{ state_attr(trigger.entity_id, 'friendly_name') }} at {{ states(trigger.entity_id) }}%"
```

### Node Offline Detection
```yaml
alias: Node Went Offline
trigger:
  - platform: state
    entity_id: sensor.meshcore_abc123_repeater1_uptime
    to: 'unavailable'
    for:
      minutes: 10
action:
  - service: notify.notify
    data:
      title: "Node Offline"
      message: "{{ state_attr(trigger.entity_id, 'friendly_name') }} is not responding"
```

### Telemetry Monitoring
```yaml
alias: High Temperature Alert
trigger:
  - platform: numeric_state
    entity_id: sensor.meshcore_def456_sensor1_ch1_temperature
    above: 30
action:
  - service: notify.notify
    data:
      message: "Temperature alert: {{ states(trigger.entity_id) }}°C"
```

## Troubleshooting

### Repeater Won't Connect
- Verify password is correct
- Check repeater is in contacts list
- Ensure repeater is powered on
- Review Home Assistant logs for login errors
- Try removing and re-adding the repeater

### Client Not Sending Telemetry
- Verify client is configured to send telemetry
- Check ACL permissions on the client
- Ensure client is within radio range
- Confirm client battery is not depleted
- Review telemetry event logs

### Excessive Backoff
- Check for consistent connection issues
- Verify radio path between nodes
- Consider increasing base update interval
- Review network congestion
- Check for repeater firmware issues

### Missing Sensors
- Sensors are created on first data reception
- Wait for at least one update cycle
- Check that telemetry is enabled (repeaters)
- Verify the node is sending expected data types
- Review debug logs for parsing errors

## Related Documentation

- [Installation](./installation.md#post-installation-configuration) - Initial setup
- [Sensors](./sensors.md#repeater-sensors) - Available sensor types
- [Events](./events.md) - Telemetry and status events