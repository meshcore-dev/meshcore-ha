"""Mesh traffic policy: the lanes every RF send crosses and the node schedule.

Every mesh request the integration makes is classified into a lane and paid
for here, and every tracked node's next poll comes from these pure helpers, so
the difference between the frozen ``legacy`` numbers and the ``governed`` ones
is a flag rather than a second code path.

``legacy`` (the default) reproduces the integration's historical arithmetic
exactly: one 20-token bucket refilling a token every 120 s, one token per mesh
request whatever it costs the mesh, lanes ignored, a denial counted as a node
failure, a backoff sized to fit five retries inside the refresh window, and
auto-disable that only ever reaches repeaters in the status loop.

``governed`` governs flood traffic, which is what actually costs the mesh. The
budget is flat per radio -- tracking more nodes shares it rather than growing
it -- and split into three independent lanes so unrouted polling can never
starve a routed poll or a message the user sent. It defers instead of failing
when a lane runs dry, and applies auto-disable to clients and telemetry too.
"""

from __future__ import annotations

import random
import time
from datetime import UTC, datetime
from typing import Any, Final, Literal

from .const import (
    CONF_TRAFFIC_POLICY,
    DEFAULT_TRAFFIC_POLICY,
    MAX_FAILURES_BEFORE_PATH_RESET,
    MAX_REPEATER_FAILURES_BEFORE_LOGIN,
    RATE_LIMITER_CAPACITY,
    RATE_LIMITER_REFILL_RATE_SECONDS,
    REPEATER_BACKOFF_BASE,
)
from .rate_limiter import TokenBucket

TrafficPolicy = Literal["legacy", "governed"]
Lane = Literal["flood", "direct", "messages"]

POLICY_LEGACY: Final[TrafficPolicy] = "legacy"
POLICY_GOVERNED: Final[TrafficPolicy] = "governed"
TRAFFIC_POLICIES: Final = (POLICY_LEGACY, POLICY_GOVERNED)

LANE_FLOOD: Final[Lane] = "flood"
LANE_DIRECT: Final[Lane] = "direct"
LANE_MESSAGES: Final[Lane] = "messages"

# The governed budget, flat per radio and shared by every tracked node:
# lane -> (burst capacity, credits refilled per hour). Flood is what actually
# costs the mesh, so it is the scarce lane; a routed request reaches one node
# over a known path and may run at volume; the user's own messages get a lane
# of their own so automatic polling can never hold them up.
GOVERNED_LANES: Final[dict[Lane, tuple[int, int]]] = {
    LANE_FLOOD: (3, 6),
    LANE_DIRECT: (20, 120),
    LANE_MESSAGES: (10, 60),
}

OP_STATUS: Final = "status"
OP_TELEMETRY: Final = "telemetry"
OP_LOGIN: Final = "login"
OP_NEIGHBOURS: Final = "neighbours"
OP_FIRMWARE: Final = "firmware"
OP_MESSAGE: Final = "message"
OP_CHANNEL_MESSAGE: Final = "channel_message"
OP_TRACE: Final = "trace"
OP_ADVERT: Final = "advert"
OP_PATH_DISCOVERY: Final = "path_discovery"

# Operations the user drives directly, whatever route they take.
MESSAGE_OPS: Final = frozenset({OP_MESSAGE, OP_CHANNEL_MESSAGE, OP_TRACE})
# Operations that reach the whole mesh however much it knows about the target.
FLOOD_OPS: Final = frozenset({OP_ADVERT, OP_PATH_DISCOVERY})

NODE_REPEATER: Final = "repeater"
NODE_CLIENT: Final = "client"

LOGIN_COOLDOWN_SECONDS: Final = 3600
LEGACY_RETRY_WINDOW_DIVISOR: Final = 31 * 2
GOVERNED_BACKOFF_CAP_SECONDS: Final = 86400
GOVERNED_BACKOFF_JITTER: Final = 0.1
SECONDS_PER_HOUR: Final = 3600


def resolve_policy(config_entry: Any) -> TrafficPolicy:
    """Return the entry's traffic policy, reading options before data."""
    value = config_entry.options.get(
        CONF_TRAFFIC_POLICY,
        config_entry.data.get(CONF_TRAFFIC_POLICY, DEFAULT_TRAFFIC_POLICY),
    )
    return POLICY_GOVERNED if value == POLICY_GOVERNED else POLICY_LEGACY


def iso_timestamp(epoch_seconds: float) -> str:
    """Format an epoch second as a UTC ISO timestamp for entity attributes."""
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat()


def classify_lane(op: str, contact: Any = None, *, path_reset: bool = False) -> Lane:
    """Return the lane an operation spends from.

    The user's own messages have a lane of their own whatever route they take.
    Everything else is direct only when the mesh already knows a route to the
    contact: an operation that reaches the whole mesh, a contact with no path,
    and the first request after a path reset all flood.
    """
    if op in MESSAGE_OPS:
        return LANE_MESSAGES
    if op in FLOOD_OPS or path_reset:
        return LANE_FLOOD
    out_path_len = contact.get("out_path_len") if contact else None
    if out_path_len is None or out_path_len < 0:
        return LANE_FLOOD
    return LANE_DIRECT


def backoff_delay(policy: TrafficPolicy, failure_count: int, interval: int) -> int:
    """Return the seconds a failing node waits before its next attempt.

    Legacy sizes a base interval so five retries fit inside half the refresh
    window and never delays past one refresh interval. Governed keeps doubling
    the configured interval up to a day, jittered so a mesh-wide outage does
    not resynchronise every node onto the same second.
    """
    if policy != POLICY_GOVERNED:
        base_interval = max(1, interval // LEGACY_RETRY_WINDOW_DIVISOR)
        return min(base_interval * (REPEATER_BACKOFF_BASE**failure_count), interval)

    delay = min(interval * (REPEATER_BACKOFF_BASE**failure_count), GOVERNED_BACKOFF_CAP_SECONDS)
    return int(delay * (1 + random.uniform(-GOVERNED_BACKOFF_JITTER, GOVERNED_BACKOFF_JITTER)))


def should_login(
    policy: TrafficPolicy, failures: int, last_login_ts: float, now: float
) -> bool:
    """Whether a repeater has failed enough, and waited long enough, to re-login."""
    if failures < MAX_REPEATER_FAILURES_BEFORE_LOGIN:
        return False
    return now - last_login_ts >= LOGIN_COOLDOWN_SECONDS


def should_reset_path(
    policy: TrafficPolicy, failures: int, has_path: bool, disabled: bool
) -> bool:
    """Whether a failing node's routing path should be rediscovered.

    Both policies honour the per-node toggle and only reset a path the node
    actually has: clearing an unknown path would force a flood.
    """
    if disabled or not has_path:
        return False
    return failures >= MAX_FAILURES_BEFORE_PATH_RESET


def denial_counts_as_failure(policy: TrafficPolicy) -> bool:
    """Whether a budget denial is recorded against the node.

    Legacy blames the node for traffic HA chose not to send. Governed defers
    the poll to the moment its lane has credit again instead.
    """
    return policy != POLICY_GOVERNED


def auto_disable_applies(
    policy: TrafficPolicy, node_type: str, *, telemetry: bool = False
) -> bool:
    """Whether inactivity auto-disable covers this node type in this loop.

    Legacy only ever disables repeaters, and only from the status loop: the
    telemetry loops keep polling a node the status loop gave up on, and a
    client is never added to the set at all. Governed covers both node types
    in both loops.
    """
    if policy == POLICY_GOVERNED:
        return True
    return node_type == NODE_REPEATER and not telemetry


class MeshBudget:
    """The credits every mesh send spends, flat per radio.

    Under ``legacy`` this is the historical single token bucket -- capacity
    20, one token every 120 s, one token per request -- and the lane a caller
    names is ignored. Under ``governed`` each lane owns a bucket from
    ``GOVERNED_LANES``, so polling a node the mesh has no route to cannot
    spend the credit a routed poll or a user's message needs.
    """

    def __init__(self, policy: TrafficPolicy) -> None:
        """Build the buckets for the policy; the rates never move after this."""
        self._policy: TrafficPolicy = policy
        self._legacy_bucket = TokenBucket(
            capacity=RATE_LIMITER_CAPACITY,
            refill_rate_seconds=float(RATE_LIMITER_REFILL_RATE_SECONDS),
        )
        self._lanes: dict[str, TokenBucket] = {
            lane: TokenBucket(capacity=capacity, refill_rate_seconds=SECONDS_PER_HOUR / per_hour)
            for lane, (capacity, per_hour) in GOVERNED_LANES.items()
        }

    @property
    def policy(self) -> TrafficPolicy:
        """Return the policy this budget is enforcing."""
        return self._policy

    def _bucket(self, lane: Lane) -> TokenBucket:
        """Return the bucket a lane spends from; legacy pools them into one."""
        if self._policy != POLICY_GOVERNED:
            return self._legacy_bucket
        return self._lanes[lane]

    def get_tokens(self) -> int:
        """Return the credits the rate-limiter sensor reports as its state."""
        return self._bucket(LANE_DIRECT).get_tokens()

    def try_consume(self, lane: Lane = LANE_DIRECT) -> bool:
        """Spend one credit in a lane; False means the send must not happen."""
        return self._bucket(lane).try_consume(1)

    def next_eligible(self, lane: Lane = LANE_DIRECT) -> float:
        """Return the seconds until this lane can pay for one more request."""
        bucket = self._bucket(lane)
        return max(0.0, (1 - bucket.get_tokens()) * bucket.refill_rate)

    def attributes(self) -> dict[str, Any] | None:
        """Return the per-lane rates for the sensor; legacy has no lanes."""
        if self._policy != POLICY_GOVERNED:
            return None
        now = time.time()
        attributes: dict[str, Any] = {"policy": self._policy}
        for lane, (capacity, per_hour) in GOVERNED_LANES.items():
            wait = self.next_eligible(lane)
            attributes[f"{lane}_credits"] = self._lanes[lane].get_tokens()
            attributes[f"{lane}_capacity"] = capacity
            attributes[f"{lane}_refill_per_hour"] = per_hour
            attributes[f"{lane}_next_eligible"] = iso_timestamp(now + wait) if wait else None
        return attributes

    def snapshot(self) -> dict[str, Any]:
        """Return the lane credits and the wall-clock second they were counted."""
        if self._policy != POLICY_GOVERNED:
            return {}
        return {
            "at": time.time(),
            "lanes": {lane: bucket.get_tokens() for lane, bucket in self._lanes.items()},
        }

    def restore(self, stored: dict[str, Any] | None) -> None:
        """Re-apply persisted credits, refilled for the time the restart took."""
        if self._policy != POLICY_GOVERNED or not stored:
            return
        elapsed = max(0.0, time.time() - float(stored.get("at", 0.0)))
        for lane, credits in (stored.get("lanes") or {}).items():
            bucket = self._lanes.get(lane)
            if bucket is None:
                continue
            bucket.tokens = min(
                bucket.capacity, int(credits) + int(elapsed / bucket.refill_rate)
            )
            bucket.last_refill = time.monotonic()
