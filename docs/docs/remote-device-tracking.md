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
   - **Update Interval**: Minimum 300 seconds (default: 7200 seconds / 2 hours)
7. Click **Submit**

**Note**: Advanced options like "Disable Path Reset" and "Disabled" can be configured after adding the repeater by editing it in **Manage Monitored Devices**.

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
   - **Update Interval**: Minimum 300 seconds (default: 7200 seconds / 2 hours)
7. Click **Submit**

**Note**: Advanced options like "Disable Path Reset" and "Disabled" can be configured after adding the client by editing it in **Manage Monitored Devices**.

The integration will:
- Send telemetry requests at the configured interval
- Create sensors for received data
- Monitor connection status

## Update Intervals

### Recommended Settings

**Repeaters:**
- High activity networks: 300-600 seconds
- Normal networks: 900-1800 seconds
- Low activity/battery conscious: 1800+ seconds

**Clients:**
- Critical sensors: 300-1200 seconds
- Standard monitoring: 1800-3600 seconds
- Battery-powered repeaters: 3600+ seconds

### Interval Considerations

- Shorter intervals provide more real-time data but increase network traffic
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

If a client doesn't respond to telemetry requests:
- Check if your node is authorized in the client's ACL
- Verify the client is within radio range
- Ensure the client has telemetry enabled

## Failure Handling

### Exponential Backoff

When updates fail, the integration implements smart exponential backoff:

1. **Dynamic Base Interval**: Calculates backoff timing to fit 5 retries within the configured interval
2. **Path Reset**: After 3 failures, automatically resets the routing path to the node (if established)
3. **Recovery**: Resets to normal interval on success

### Automatic Re-login

For repeaters, the integration automatically re-logs in after 5 consecutive failures.

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

### Reliability Tracking

Each tracked node provides reliability metrics:

- **Request Successes**: Total count of successful requests (login, status, telemetry)
- **Request Failures**: Total count of failed requests (timeouts, errors, exceptions)
- **Routing Path**: Current path through the mesh network
- **Path Length**: Number of hops to reach the node

These sensors help monitor network health and identify problematic nodes or routing issues.

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
   - **Update Interval**: How often to poll the device
   - **Password**: Authentication password (repeaters only)
   - **Telemetry Collection**: Enable/disable telemetry requests (repeaters only)
   - **Disable Path Reset**: Prevent automatic path resets on failures
   - **Disabled**: Temporarily stop all updates to this device
5. Click **Submit**

#### Device Options Explained

**Disable Path Reset:**
By default, after 3 consecutive failures, the integration automatically resets the routing path to the node. Enable this option to prevent path resets if you have a stable, manually-configured path using the `update_contact` command.

**Disabled:**
Temporarily stop all status, telemetry, and login requests to this device without removing it from your configuration. Useful when:
- A node is temporarily offline for maintenance
- You want to reduce network traffic temporarily
- Testing network performance without a specific node
- A device is causing excessive failures

When disabled, the device and its sensors remain in Home Assistant but no updates are requested.

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

### Rate Limiting

The integration implements a **token bucket rate limiter** to prevent overwhelming the mesh network with requests:

**Configuration:**
- **Burst Capacity**: 20 tokens (allows up to 20 rapid requests)
- **Refill Rate**: 1 token per 3 minutes (180 seconds)
- **Average Rate**: ~0.33 requests per minute (20 requests per hour)

**How It Works:**

1. Each mesh request (login, status, telemetry) consumes 1 token
2. The bucket starts full with 20 tokens, allowing immediate bursts
3. Tokens refill gradually at 1 per 3 minutes
4. If no tokens are available, the request is skipped (not queued)
5. Skipped requests count as failures and trigger exponential backoff

**Practical Impact:**

- Initial startup can process 20 requests rapidly
- Sustained operation limited to ~20 requests/hour across all tracked devices
- With default 2-hour update intervals:
  - 10 repeaters = 5 requests/hour (well within limit)
  - 20 devices = 10 requests/hour (manageable)
  - 30+ devices may experience rate limiting

**When Rate Limited:**
- Requests are skipped and logged as debug messages
- The update is counted as a failure
- Exponential backoff increases retry delay
- Network traffic is protected from excessive load

**Adjusting for Large Networks:**

If you're monitoring many devices and experiencing rate limiting:
1. Increase update intervals (3-4 hours instead of 2)
2. Disable telemetry on less-critical repeaters
3. Use the "Disabled" option for devices that don't need constant monitoring
4. Stagger device addition to avoid burst consumption

### Best Practices

Make the update interval as high as you can to support your needs to avoid excess mesh traffic.

### Troubleshooting High Failure Rates

If nodes frequently fail to update:
1. Check radio signal strength (RSSI/SNR)
2. Verify node is powered and online
3. Check for rate limiting (review debug logs)
4. Increase update interval
5. Check for network congestion
6. Review repeater passwords
7. Verify client ACL permissions
8. Set a direct path to the remote node via the `update_contact` command if you have a stable path
9. Enable "Disable Path Reset" if you have a manually-configured stable path
10. Temporarily disable problematic nodes to isolate network issues

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

### Rate Limiting Issues
To check if rate limiting is affecting your network:
- Monitor the **Rate Limiter Tokens** sensor (shows current available tokens)
- If tokens frequently reach 0, you're hitting the rate limit
- Calculate your total requests per hour (devices × updates/hour)
- Ensure you're under 20 requests/hour sustained
- Increase update intervals on less critical devices
- Disable telemetry collection where not needed
- Consider temporarily disabling some devices

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