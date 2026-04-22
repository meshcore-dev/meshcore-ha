---
sidebar_position: 2.5
title: Repeater Neighbors
---

# Repeater Neighbors

Each repeater in a MeshCore network keeps an in-firmware list of other nodes it has recently heard. The integration can surface that list to Home Assistant as a pair of sensors per neighbor — signal quality and activity — giving you a view of the mesh topology and health from the perspective of each repeater.

This feature is **disabled by default** on every repeater. It has to be turned on explicitly for each repeater that you want to track neighbors for.

## What gets created

When enabled for a repeater, every distinct neighbor the repeater reports produces two sensors, both attached to that repeater's existing HA device:

### SNR sensor

- **Entity**: `sensor.meshcore_<repeater>_neighbor_<neighbor>`
- **Unit**: dB
- **State class**: Measurement
- **Value**: Most recent signal-to-noise ratio the repeater measured from that neighbor
- **Availability**: Becomes unavailable if the neighbor has not been heard in the last 72 hours

### Activity sensor (`seen_48h`)

- **Entity**: `sensor.meshcore_<repeater>_neighbor_<neighbor>_seen`
- **Unit**: count
- **State class**: Measurement (the value decreases as old sightings age out)
- **Value**: Number of times this neighbor was heard by the repeater in the last 48 hours
- **Availability**: Same 72-hour threshold as the SNR sensor

The friendly name for both sensors resolves to the neighbor's advertised name when available, and falls back to a public-key prefix otherwise. Names update if the neighbor later shows up in the contact list.

## Enabling neighbors for a repeater

1. Go to **Settings → Devices & Services → Meshcore → Configure**
2. Open the per-repeater subscription for the repeater you want to track
3. Toggle **Enable Neighbor Entities**
4. Save

The next repeater update cycle will query the firmware for the neighbor list and create sensors. Disabling the toggle removes all neighbor sensors for that repeater, clears in-memory state, and deletes persisted data.

## Airtime cost

Enabling neighbor entities for a repeater adds a small, bounded amount of mesh traffic. The integration sends one binary request to the repeater on every poll cycle (default: every 2 hours, set by **Update Interval** on the repeater subscription) and the repeater replies with its current neighbor list.

**Per cycle, per enabled repeater:**

- **Request**: ~10 bytes of binary payload.
- **Response**: 4-byte header plus ~11 bytes per neighbor in the list (6-byte pubkey prefix + 4-byte last-heard + 1-byte SNR). A repeater with 25 neighbors returns ~280 bytes; 50 neighbors returns ~550 bytes.

**How this travels on the mesh.** The request and response are direct, path-routed messages between your Home Assistant gateway node and the repeater — not flood adverts. Only the nodes on the established path between the gateway and the repeater re-transmit the packets. The footprint therefore scales with the hop count to that specific repeater, not with the size of the mesh.

- **Zero-hop repeater** (your gateway hears the repeater directly): only those two nodes are on-air for the exchange. The cheapest case.
- **N-hop repeater**: each of the N intermediate repeaters also re-transmits. Airtime events per cycle are roughly `2 × (N + 1)`.

In practical terms, the cost per cycle is comparable to sending one extra telemetry request to the same repeater, and substantially lower than a single flood advert or a group-channel message — both of which are re-transmitted by every repeater in range of any participant regardless of who the target is.

**Cadence.** At the default 2-hour `Update Interval` this is about one exchange every 2 hours per enabled repeater, or ~12 per day per repeater. Shortening `Update Interval` scales the cost linearly.

**Shared rate limiter.** The neighbor request consumes one token from the integration's shared mesh-request rate limiter — the same token bucket used for repeater status polls, login retries, and tracked-node telemetry (surfaced as the `rate_limiter_tokens` sensor). If the bucket is empty when a neighbor fetch is due, the fetch is skipped for that cycle and retries on the next poll; enabling neighbors therefore slightly increases contention for this shared pool in deployments that already track many repeaters and clients.

**Firmware requirement.** The repeater must run firmware that supports the neighbors binary request (MeshCore firmware ≥ 1.14.0). Older firmware will not respond and the sensors will remain unavailable until the repeater is updated.

## Persistence

Neighbor data is stored through Home Assistant's `Store` helper. After a restart, all neighbor sensors are recreated from disk and the 48-hour activity window continues seamlessly. The integration guards `seen_timestamps` against the inflated `secs_ago` values the firmware reports during its own startup, so the rolling window remains accurate across both HA and repeater reboots.

## Integration-level stale cleanup

Separate from the per-repeater enable toggle, an integration-wide option automatically removes neighbors that haven't been heard in a configurable number of days. This is also **disabled by default**.

- **Auto-Remove Stale Neighbors** — on/off
- **Stale Neighbor Threshold (days)** — 1 to 365, default 7

When enabled, a daily task removes matching neighbors from every tracked repeater. The sensors are removed from the entity registry, in-memory state is cleared, and the persisted data is rewritten. This prevents the sensor count from growing without bound in mobile or dense deployments where nodes come and go.

Configure under **Configure → Global Settings**.

## Use cases

**Mesh health at a glance**
: Each repeater's neighbor SNR sensors form a snapshot of its local radio environment. Graphing them over time shows degradation, new arrivals, or links dropping out before they become network-wide problems.

**Activity-based alerts**
: The `seen_48h` sensors make it easy to flag a repeater that has stopped hearing a neighbor it normally hears dozens of times a day.

**Coverage mapping**
: Combining neighbor lists from multiple repeaters gives you a first-order view of which nodes are within direct earshot of which, without having to correlate RX_LOG entries yourself.

## Notes

- Sensors are only created for repeaters the integration actively tracks (i.e., ones you've subscribed to). Neighbors of a repeater you haven't subscribed to will not appear.
- The neighbor list comes from the repeater firmware's own tracking. Nodes the repeater has never heard will not appear even if they exist elsewhere in the mesh.
- Turning the per-repeater toggle off cleans up immediately — you don't need to restart Home Assistant.
