---
sidebar_position: 9
title: Map Auto Uploader
---

# Map Auto Uploader (map.meshcore.io)

When enabled, the integration automatically uploads repeater and room server adverts to [map.meshcore.io](https://map.meshcore.io) when your Companion hears them. No separate companion node or map uploader bot is required.

## Overview

- **Same connection** — Uses your existing USB, BLE, or TCP connection to the MeshCore node
- **Automatic** — Adverts you receive are uploaded in the background
- **Community map** — Uploaded nodes appear on the official MeshCore map
- **Replay protection** — Built-in cooldown prevents duplicate uploads
- **Updates and cleanup** — Nodes not seen by any uploader for 30 days are eventually removed from the map

## Enable Map Auto Uploader

1. Go to **Settings** → **Devices & Services**
2. Open your **MeshCore** integration → **Configure** → **Global Settings**
3. Enable **Enable Map Auto Uploader (map.meshcore.io)**

Map Auto Uploader is **off by default**.

## Requirements

- **Private key export** — Firmware must have `ENABLE_PRIVATE_KEY_EXPORT=1`. If disabled, Map Auto Uploader cannot start. Check logs for `Private key export command failed`.

## How It Works

1. Your Companion receives adverts from repeaters and room servers on the mesh
2. The integration verifies each advert and checks for replay
3. Valid adverts are signed and uploaded to map.meshcore.io
4. Nodes appear on the [official map](https://map.meshcore.io) for the community

## Troubleshooting

1. **Enable in Global Settings** — Ensure the option is enabled (see above)
2. **Check private key export** — Firmware must have `ENABLE_PRIVATE_KEY_EXPORT=1`
3. **Verify connectivity** — Your node must receive adverts from repeaters or room servers
4. **Check logs** — Look for `meshcore` or Map Auto Uploader messages

Common log messages:

- `Map Auto Uploader: cannot sign (private key export disabled?)` — Firmware does not allow private key export
- `ERR_PARAMS_INVALID` — Advert params rejected by the map API
- `Replay cooldown` — Same node was recently uploaded; wait before retrying

## For more info

- [map.meshcore.io-uploader](https://github.com/recrof/map.meshcore.io-uploader) — Standalone bot (Node.js)
- [map.meshcore.io](https://map.meshcore.io) — Official map (live site)
- [meshcore-dev/map.meshcore.io](https://github.com/meshcore-dev/map.meshcore.io) — Map frontend source (GitHub)

Many thanks to [recrof](https://github.com/recrof) for both projects.
