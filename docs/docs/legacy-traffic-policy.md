---
sidebar_position: 21
title: Legacy traffic policy
---

# Legacy traffic policy

:::caution Deprecated
Legacy is deprecated. Only entries that used it in 2.x keep it. A future release will remove it. After you change an entry to Governed, the UI does not offer Legacy again.
:::

Legacy keeps the budget and the retry timing of 2.x. New entries use Governed. See [Mesh Traffic Policy](traffic-policy.md).

At each startup or reload, an entry on Legacy logs this warning:

```
The Legacy traffic policy is deprecated and will be removed in a future release. Select Governed in Global Settings > Mesh Traffic Policy.
```

## How Legacy differs from Governed

| Item | Legacy behavior |
|---|---|
| Budget | One bucket of 20 credits. It gets 1 credit every 120 seconds (approximately 30 each hour). |
| Cost | 1 credit for each automatic request. A full neighbor scan costs 1 credit. |
| Service calls, forms and buttons | Not metered |
| Empty budget | A denied status or telemetry poll counts as a failure, and the node backs off. A denied login also starts a backoff. The integration skips a denied neighbor scan. |
| Retry spacing | `interval / 62 x 2^failures`, never more than the poll interval, for all retries |
| Route reset | After 3 failures, the integration resets the route. The next poll floods. There is no route healing. |
| Auto-disable | Repeaters only, and status polls only. The log line ends with `This will reset on restart.` To resume the node, restart Home Assistant or reload the entry. |
| State across restarts | Not saved. A restart resets the budget, the schedules and the auto-disabled nodes. |
| Request Rate Limiter sensor | Shows the single bucket. It has no attributes. |

## Before you change to Governed

Do these steps before the change. You cannot change back to Legacy.

1. Add `continue_on_error: true` to the automation actions that send messages or mesh commands. Under Governed, service calls use credit. If the lane is empty, the call fails with `Mesh traffic <lane> lane is empty; try again in <seconds> seconds`.
2. Update the dashboards and alerts that use the **Request Rate Limiter** sensor. Its state changes from the single bucket to the credits of the direct lane. See [Monitor the budget](traffic-policy.md#watching-the-budget).

Under Governed, [auto-disable](traffic-policy.md#auto-disable) also covers clients and stays after a restart. A node resumes when you edit it or when the companion hears its next advert.

**Add Repeater Station** also uses credit under Governed. If the lane is empty, the form shows an error. Wait, then submit again.

## Change to Governed

1. Go to **Settings > Devices & services > MeshCore > Configure > Global Settings**.
2. Set **Mesh Traffic Policy** to **Governed**.
3. Select **Submit**.

The change applies immediately, without a reload.
