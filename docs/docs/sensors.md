---
sidebar_position: 2
title: Sensors
---

# Sensors

The Meshcore Home Assistant integration provides comprehensive monitoring of your mesh network through various sensor categories. Sensors are automatically discovered and created based on the devices and data available in your network.

## Sensor Categories

### Device Sensors (Main Node)
These sensors monitor the main Meshcore device connected via USB, TCP, or BLE.

#### Core Status
- **Node Status** - Connection status (online/offline)
  - Example: `sensor.meshcore_abc123_node_status_mynode`

#### Power Management
- **Battery Voltage** - Battery voltage in volts
  - Example: `sensor.meshcore_abc123_battery_voltage_mynode`
  - Unit: V (2 decimal precision)
  - Device Class: Voltage
  
- **Battery Percentage** - Battery level percentage
  - Example: `sensor.meshcore_abc123_battery_percentage_mynode`
  - Unit: % (0-100)
  - Device Class: Battery

#### Network Information
- **Node Count** - Number of nodes in the mesh network
  - Example: `sensor.meshcore_abc123_node_count_mynode`
  
- **TX Power** - Transmission power level
  - Example: `sensor.meshcore_abc123_tx_power_mynode`
  - Unit: dBm
  - Device Class: Signal Strength

#### Radio Configuration
- **Frequency** - Radio operating frequency
  - Example: `sensor.meshcore_abc123_frequency_mynode`
  - Unit: MHz (3 decimal precision)
  
- **Bandwidth** - Radio bandwidth
  - Example: `sensor.meshcore_abc123_bandwidth_mynode`
  - Unit: kHz (1 decimal precision)
  
- **Spreading Factor** - LoRa spreading factor setting
  - Example: `sensor.meshcore_abc123_spreading_factor_mynode`

#### Location
- **Latitude** - Node latitude coordinate
  - Example: `sensor.meshcore_abc123_latitude_mynode`
  
- **Longitude** - Node longitude coordinate
  - Example: `sensor.meshcore_abc123_longitude_mynode`

### Contact Sensors (Remote Clients)
These sensors monitor remote nodes discovered in the mesh network.

#### Node Status
- **Status** - Node connectivity status
  - Entity: `sensor.meshcore_<pubkey>_<name>_status`

#### Power & Signal
- **Battery** - Remote node battery voltage
  - Unit: V (2 decimal precision)
  - Device Class: Voltage
  
- **Battery Percentage** - Calculated battery percentage
  - Unit: % (0-100)
  - Device Class: Battery
  
- **Last RSSI** - Last received signal strength
  - Unit: dBm
  
- **Last SNR** - Last signal-to-noise ratio
  - Unit: dB (1 decimal precision)

#### Routing & Network Topology  
- **Routing Path** (`out_path`) - Current routing path to reach this client
  - Shows the sequence of node public key prefixes used to route messages
  - Example: "abc123,def456" (message routes through abc123 then def456)  
  - Empty if client is directly reachable
  
- **Path Length** (`out_path_len`) - Number of hops to reach this client
  - Unit: hops
  - Value: Number of intermediate nodes (0 = direct, -1 = unreachable)
  - State Class: Measurement

### Repeater Sensors
Repeaters provide detailed operational statistics when subscribed.

#### Power & Uptime
- **Battery Voltage** (`bat`) - Repeater battery voltage
  - Unit: V (converted from mV)
  - Device Class: Voltage
  
- **Battery Percentage** - Calculated from voltage
  - Unit: % (0-100)
  - Device Class: Battery
  
- **Uptime** - Operating time
  - Unit: min (converted from seconds)
  - Suggested Unit: days
  - Attributes: Human-readable format

#### Airtime Metrics
- **Airtime** - Total transmission time
  - Unit: min (1 decimal precision)
  
- **RX Airtime** - Total receive time
  - Unit: min (1 decimal precision)
  
- **Airtime Utilization** - Percentage of time transmitting
  - Unit: % (1 decimal precision)
  - Device Class: Power Factor
  
- **RX Airtime Utilization** - Percentage of time receiving
  - Unit: % (1 decimal precision)
  - Device Class: Power Factor

#### Message Counters
- **Messages Sent** (`nb_sent`) - Total messages transmitted
- **Messages Received** (`nb_recv`) - Total messages received
- **Sent Flood Messages** (`sent_flood`) - Broadcast messages sent
- **Sent Direct Messages** (`sent_direct`) - Direct messages sent
- **Received Flood Messages** (`recv_flood`) - Broadcast messages received
- **Received Direct Messages** (`recv_direct`) - Direct messages received

All counters use State Class: Total Increasing

#### Queue & System
- **TX Queue Length** (`tx_queue_len`) - Messages waiting to transmit
  
- **Noise Floor** (`noise_floor`) - Background radio noise level
  - Unit: dBm
  
- **Full Events** (`full_evts`) - Queue saturation events
  - State Class: Total Increasing

#### Routing & Network Topology
- **Routing Path** (`out_path`) - Current routing path to reach this node
  - Shows the sequence of node public key prefixes used to route messages
  - Example: "abc123,def456" (message routes through abc123 then def456)
  - Empty if node is directly reachable
  
- **Path Length** (`out_path_len`) - Number of hops to reach this node
  - Unit: hops
  - Value: Number of intermediate nodes (0 = direct, -1 = unreachable)
  - State Class: Measurement

#### Duplicate Detection
- **Direct Duplicates** (`direct_dups`) - Filtered direct message duplicates
- **Flood Duplicates** (`flood_dups`) - Filtered broadcast duplicates

Both use State Class: Total Increasing

#### Rate Metrics
All message counters automatically generate rate sensors:
- **Messages Sent/Received Rate** - msg/min
- **Direct/Flood Messages Rate** - msg/min
- **Duplicate Messages Rate** - msg/min

All rates use 1 decimal precision.

### Telemetry Sensors (Cayenne LPP)
Automatically discovered from telemetry data using Cayenne LPP format.

#### Environmental
- **Temperature** (Type 103)
  - Unit: °C (1 decimal precision)
  - Device Class: Temperature
  
- **Humidity** (Type 104)
  - Unit: % (1 decimal precision)
  - Device Class: Humidity
  
- **Illuminance** (Type 101)
  - Unit: lx
  - Device Class: Illuminance
  
- **Presence** (Type 102)
  - Binary state sensor

#### Electrical
- **Voltage** (Type 116)
  - Unit: V (2 decimal precision)
  - Device Class: Voltage
  - Note: Channel 1 voltage on clients creates battery percentage sensor
  
- **Current** (Type 117)
  - Unit: A (2 decimal precision)
  - Device Class: Current

#### Analog/Digital I/O
- **Digital Input** (Type 0) - Binary state
- **Digital Output** (Type 1) - Binary state
- **Analog Input** (Type 2) - V, 2 decimal precision
- **Analog Output** (Type 3) - V, 2 decimal precision

#### Multi-Value Sensors
- **Accelerometer** (Type 113)
  - Creates separate X, Y, Z sensors
  - Unit: G (3 decimal precision)
  
- **Color** (Type 135)
  - Creates separate R, G, B sensors

#### Generic
- **Generic Sensor** (Type 100)
  - State Class: Measurement

### Binary Sensors

#### Contact Status (Diagnostic)
Binary sensors showing node freshness:
- **Entity**: `binary_sensor.meshcore_<pubkey>_<name>_status`
- **Device Class**: Connectivity
- **Category**: Diagnostic
- **States**: 
  - On = Fresh (recent activity within 12 hours)
  - Off = Stale (no recent activity)

#### Message Tracking
Binary sensors created on first message:

**Channel Messages**
- **Entity**: `binary_sensor.meshcore_<device>_ch_<number>_messages`
- **Device Class**: Connectivity
- **Created**: On first message in channel

**Contact Messages**
- **Entity**: `binary_sensor.meshcore_<pubkey>_messages`
- **Device Class**: Connectivity
- **Created**: On first message from contact

### Device Trackers (GPS)

GPS telemetry automatically creates device tracker entities:
- **Entity**: `device_tracker.meshcore_<pubkey>_<name>_gps`
- **Source Type**: GPS
- **Attributes**:
  - Latitude
  - Longitude
  - Altitude (if available)
  - Accuracy (if available)
  - Node information

## Automatic Discovery

Sensors are created dynamically as data becomes available:

1. **Initial Connection** - Core device sensors created immediately
2. **Network Discovery** - Contact sensors added as nodes are discovered
3. **First Telemetry** - Telemetry sensors created on first data reception
4. **First Message** - Message binary sensors created on activity
5. **GPS Data** - Device trackers created on first GPS telemetry

## Sensor Naming Convention

Consistent naming patterns for easy identification:
- **Root node sensors**: `sensor.meshcore_<pubkey>_<sensor_name>_<device_name>`
- **Remote nodes**: `sensor.meshcore_<pubkey>_<node_name>_<sensor_name>`
- **Telemetry**: `sensor.meshcore_<pubkey>_<device_name>_ch<number>_<type>_<field>`
- **GPS trackers**: `device_tracker.meshcore_<pubkey>_<name>_gps`

## Data Freshness

Sensors implement freshness tracking:
- **Tracked device sensors** (repeaters/clients/telemetry): Mark unavailable after 3x the configured update interval without data
- **Contact binary sensors**: Show stale after 12 hours without advertisement
- **GPS trackers**: Update on each telemetry reception

## Entity Organization

All entities are organized under appropriate devices:
- **Meshcore Device** - Main node sensors
- **Repeater Devices** - Per-repeater statistics and telemetry
- **Client Devices** - Per-client telemetry sensors
- **Contact Devices** - Remote node diagnostic sensors

## Performance Considerations

- Telemetry updates are batched to reduce database writes
- Rate calculations use sliding windows for accuracy
- Sensors mark unavailable rather than showing stale data
- Binary sensors minimize state changes
- Duplicate telemetry is filtered automatically

## Usage Examples

### Monitor Battery Health
```yaml
sensor:
  - platform: template
    sensors:
      mesh_low_battery_count:
        friendly_name: "Low Battery Nodes"
        value_template: >
          {{ states.sensor 
             | selectattr('entity_id', 'match', 'sensor.meshcore_.*_battery_percentage')
             | selectattr('state', 'lt', '20')
             | list | count }}
```

### Track Network Activity
```yaml
sensor:
  - platform: template
    sensors:
      mesh_message_rate:
        friendly_name: "Network Message Rate"
        unit_of_measurement: "msg/min"
        value_template: >
          {{ states('sensor.meshcore_abc123_repeater1_nb_recv_rate') | float(0) +
             states('sensor.meshcore_abc123_repeater1_nb_sent_rate') | float(0) }}
```