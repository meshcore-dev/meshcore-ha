---
sidebar_position: 18
title: Map Auto Uploader
---

# Map Auto Uploader (map.meshcore.io)

The Map Auto Uploader sends node adverts that your companion receives to [map.meshcore.io](https://map.meshcore.io), the community MeshCore map. It uses the existing USB, BLE or TCP connection. You do not need a second device or a separate uploader bot.

- The integration uploads adverts from repeaters, room servers and sensor nodes. It does not upload adverts from clients.
- Each upload contains the raw advert packet and the radio settings of your companion (frequency, bandwidth, spreading factor, coding rate).
- The integration signs each upload with the private key of your companion. It ignores adverts with a bad signature.

## Requirements

- The companion firmware must allow private key export (`ENABLE_PRIVATE_KEY_EXPORT=1`).
- Home Assistant must reach `https://map.meshcore.io` over the internet.

## Enable the uploader

The uploader is off by default.

1. Go to **Settings > Devices & services > MeshCore > Configure**.
2. Select **Global Settings**.
3. Enable **Enable Map Auto Uploader (map.meshcore.io)**.
4. Select **Submit**.

The change applies at once, without a reload.

## Upload frequency

The uploader sends each node to the map a maximum of one time per hour. After a successful upload, it ignores adverts from that node for 1 hour. A failed upload does not start the 1-hour period.

## Private key

The uploader asks the companion for its private key before the first upload. If the companion refuses or does not answer, the uploader logs one warning and does not ask again until the companion connects again. After you change the firmware, reconnect the companion or reload the integration.

## Log messages

Set the `custom_components.meshcore` logger to `info` to see uploads. Set it to `debug` to see why the uploader skips adverts.

| Level | Message | Meaning |
|---|---|---|
| WARNING | `Map Auto Uploader: cannot sign, the node did not supply its private key (firmware needs ENABLE_PRIVATE_KEY_EXPORT=1). Not asking again until the next connection.` | Key export is disabled or failed. Enable private key export in the firmware. |
| INFO | `Map Auto Uploader: uploaded node, response: {...}` | The map API accepted the upload. |
| WARNING | `Map Auto Uploader: params rejected (freq=..., bw=..., sf=..., cr=...) - <error>` | The map API did not accept the radio settings. |
| WARNING | `Map Auto Uploader: upload failed: <error>` | The map API returned an error, or a network error stopped the upload. |
| DEBUG | `Map Auto Uploader: too soon to reupload <pubkey prefix>` | The node uploaded less than 1 hour ago. |
| DEBUG | `Map Auto Uploader: no radio params yet, skipping` | The radio settings of the companion are not known yet. |

## More information

- [meshcore-dev/map.meshcore.io](https://github.com/meshcore-dev/map.meshcore.io): the source code of the map.
- [map.meshcore.io-uploader](https://github.com/recrof/map.meshcore.io-uploader): a standalone uploader bot (Node.js).
- [MQTT Upload](mqtt.md): the auth token of an MQTT broker also uses private key export.

Thanks to [recrof](https://github.com/recrof) for the map and the uploader bot.
