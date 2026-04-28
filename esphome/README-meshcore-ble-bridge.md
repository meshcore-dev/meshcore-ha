# MeshCore BLE Bridge for ESPHome

This external ESPHome component keeps the BLE connection on the ESP32, where
`ble_client` can answer the MeshCore node's passkey request. It then exposes the
authenticated MeshCore BLE session as a small TCP server using the same framing
that `meshcore-py` already expects for TCP connections.

Use the stock MeshCore Home Assistant integration as a TCP connection:

- host: the ESPHome device hostname or IP, for example `meshcore-ble-link.local`
- port: `5000`

Do not configure this MeshCore node through Home Assistant Bluetooth Proxy. The
proxy creates its own BLE connection and cannot reuse the authenticated
`ble_client` session.

## ESPHome YAML

Keep the `ble_client` section that already authenticates with the node, then add:

```yaml
external_components:
  - source:
      type: local
      path: components
    components: [meshcore_ble_bridge]

meshcore_ble_bridge:
  id: meshcore_bridge
  ble_client_id: meshcore_ble
  port: 5000
  force_encryption: true
  wait_for_auth: true
```

For a PIN-protected MeshCore node, keep this pattern:

```yaml
logger:
  level: DEBUG
  logs:
    esp32_ble_tracker: DEBUG
    esp32_ble_client: DEBUG
    ble_client: DEBUG
    meshcore_ble_bridge: DEBUG

esp32_ble:
  io_capability: keyboard_only

ble_client:
  - mac_address: AC:A7:04:07:CE:21
    id: meshcore_ble
    auto_connect: true
    on_passkey_request:
      then:
        - ble_client.passkey_reply:
            id: meshcore_ble
            passkey: 771519
```

The bridge's `force_encryption: true` setting replaces the previous
`on_connect` lambda that called `esp_ble_set_encryption` with MITM encryption.

Avoid `VERY_VERBOSE` for `esp32_ble_tracker` during normal testing. On an ESP32
with many BLE advertisements nearby, the scan log flood can trip the task
watchdog before the MeshCore client even connects.

`bluetooth_proxy` is intentionally not used for this MeshCore node. If you need a
general Home Assistant Bluetooth proxy, use another ESP32 or be prepared for BLE
slot/resource conflicts.

## Expected logs

After flashing, the ESPHome logs should show:

```text
BLE authentication complete
BLE handles ready: RX=... TX=... CCCD=...
MeshCore BLE bridge ready
MeshCore TCP bridge listening on port 5000
```

Then add or reconfigure MeshCore-HA with connection type `TCP`, using the ESPHome
device address and port `5000`.
