![MeshCore Banner](images/meshcore-bg.png)

# MeshCore for Home Assistant

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=meshcore)
[![Add Repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=meshcore-dev&repository=meshcore-ha&category=integration)

This custom integration connects Home Assistant to a MeshCore companion over USB, BLE or TCP. The companion is the radio that sends and receives on the mesh for Home Assistant. Use it to monitor and control the nodes of your MeshCore mesh.

The integration uses the [meshcore-py](https://github.com/meshcore-dev/meshcore_py) library.

## Documentation

The full documentation is at **[meshcore-dev.github.io/meshcore-ha](https://meshcore-dev.github.io/meshcore-ha/)**. If you upgrade from 2.x, read [Upgrade to 3.0](https://meshcore-dev.github.io/meshcore-ha/docs/ha/upgrade-3.0) first.

## What is new in 3.0

- **Mesh Traffic Policy.** New installs get a fixed budget for flood traffic for each companion. This protects the shared mesh. See [Mesh Traffic Policy](https://meshcore-dev.github.io/meshcore-ha/docs/ha/traffic-policy).
- **Most options apply without a reload**, including add, edit and remove of tracked nodes.
- **Event changes.** Each `meshcore_*` event has `entry_id` and `device_id`. Outgoing message events changed, and a failed send fires `meshcore_message_send_failed`.
- **Secrets stay hidden.** Events and MQTT raw payloads do not contain channel secrets, unless you enable **Expose Node Secrets in Events**.

## Requirements

- Home Assistant 2025.6.0 or later.
- A MeshCore companion with the companion firmware for your connection type (USB, Bluetooth or WiFi).
- For BLE: a direct Bluetooth adapter on the host. A Bluetooth proxy does not work with PIN pairing. BLE has had less testing than USB and TCP.

## Installation

1. In HACS, add `https://github.com/meshcore-dev/meshcore-ha` as a custom repository of type **Integration**.
2. Download **MeshCore**.
3. Restart Home Assistant.
4. Go to **Settings > Devices & services > Add Integration**.
5. Select **MeshCore**.
6. Select the connection type.
7. Complete the form.

For manual installation and all options, see [Installation](https://meshcore-dev.github.io/meshcore-ha/docs/ha/installation). For the tasks after setup, see [First steps](https://meshcore-dev.github.io/meshcore-ha/docs/ha/first-steps).

## Lovelace card

For a companion Lovelace card that shows MeshCore node data, see [meshcore-card](https://github.com/jpettitt/meshcore-card).

## Share the companion with a phone

To let a phone get the chat messages while Home Assistant monitors repeaters, disable **Global Settings > Retrieve queued incoming messages**. Status, telemetry, contacts and commands continue to work. See [Share the companion with a phone](https://meshcore-dev.github.io/meshcore-ha/docs/ha/messaging#sharing-a-companion-with-a-phone).

## Contact Discovery Mode

**Contact Discovery Mode** controls what the integration keeps for discovered contacts: Entity per contact (default), Data only or Disabled. On a large mesh, use Data only to prevent hundreds of entities. See [Contact Discovery Mode](https://meshcore-dev.github.io/meshcore-ha/docs/ha/contacts#contact-discovery-mode).

## MQTT upload

Configure a maximum of 4 brokers in **Manage MQTT Brokers**. The auth token mode needs firmware that permits private key export. Broker changes apply without a reload. See [MQTT Upload](https://meshcore-dev.github.io/meshcore-ha/docs/ha/mqtt).

## Map Auto Uploader (map.meshcore.io)

When enabled in Global Settings, the integration uploads the repeater, room server and sensor adverts that your companion receives to [map.meshcore.io](https://map.meshcore.io). The firmware must have `ENABLE_PRIVATE_KEY_EXPORT=1`. For a standalone uploader, see [map.meshcore.io-uploader](https://github.com/recrof/map.meshcore.io-uploader).

## Self Diagnostics

When enabled, the integration shows the statistics of the companion as 14 sensors and 3 radio fault binary sensors. The queries go to the companion only and use no airtime on the mesh. Each fault flag stays on until the companion reboots. See [Sensors](https://meshcore-dev.github.io/meshcore-ha/docs/ha/sensors#self-diagnostics-sensors).

## Development

### Local development

1. Clone this repository.
2. Copy `custom_components/meshcore` into the configuration directory of your Home Assistant.
3. Restart Home Assistant.
4. Add the integration in the UI.

### Tests

The tests have two tiers. Run each tier as a separate pytest command.

```bash
pip install -r requirements-test.txt
pytest tests

pip install -r requirements-test-integration.txt
pytest tests_integration
```

## Support

- Talk with the community on [Discord](https://discord.com/channels/1495203904898728149/1508972219202535475).
- Report problems on [GitHub Issues](https://github.com/meshcore-dev/meshcore-ha/issues).
- Send code and documentation changes as pull requests.

## License

This project uses the MIT License. See the LICENSE file.
