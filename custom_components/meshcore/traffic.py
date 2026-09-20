"""Mesh traffic policy: one budget and one set of node-schedule decisions.

Every RF send the integration makes crosses the budget here, and every tracked
node's next poll comes from these pure helpers, so the difference between the
frozen ``legacy`` numbers and the ``governed`` ones is a flag rather than a
second code path.

``legacy`` (the default) reproduces the integration's historical arithmetic
exactly: a 20-token bucket refilling one token every 120 s, one token per mesh
request whatever it costs the mesh, a denial counted as a node failure, a
backoff sized to fit five retries inside the refresh window, and auto-disable
that only ever reaches repeaters in the status loop.

``governed`` sizes the budget to the mesh being tracked, charges flood-routed
traffic what it costs, keeps credits in reserve for the user's own commands,
defers instead of failing when credit runs out, and applies auto-disable to
clients and to the telemetry loops as well.
"""

from __future__ import annotations

import random
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

POLICY_LEGACY: Final[TrafficPolicy] = "legacy"
POLICY_GOVERNED: Final[TrafficPolicy] = "governed"
TRAFFIC_POLICIES: Final = (POLICY_LEGACY, POLICY_GOVERNED)

NODE_REPEATER: Final = "repeater"
NODE_CLIENT: Final = "client"

LOGIN_COOLDOWN_SECONDS: Final = 3600
LEGACY_RETRY_WINDOW_DIVISOR: Final = 31 * 2
GOVERNED_BACKOFF_CAP_SECONDS: Final = 86400
GOVERNED_BACKOFF_JITTER: Final = 0.1

COST_DIRECT: Final = 1
COST_FLOOD: Final = 8
COST_NEIGHBOUR_PAGE: Final = 1
COST_LOGIN_STATUS: Final = 2
INTERACTIVE_RESERVE: Final = 6

GOVERNED_CAPACITY_BASE: Final = 12
GOVERNED_CAPACITY_PER_NODE: Final = 2
GOVERNED_CAPACITY_RANGE: Final = (20, 48)
GOVERNED_REFILL_PER_NODE_PER_HOUR: Final = 6
GOVERNED_REFILL_RANGE_PER_HOUR: Final = (24, 96)
SECONDS_PER_HOUR: Final = 3600


def resolve_policy(config_entry: Any) -> TrafficPolicy:
    """Return the entry's traffic policy, reading options before data."""
    value = config_entry.options.get(
        CONF_TRAFFIC_POLICY,
        config_entry.data.get(CONF_TRAFFIC_POLICY, DEFAULT_TRAFFIC_POLICY),
    )
    return POLICY_GOVERNED if value == POLICY_GOVERNED else POLICY_LEGACY


def budget_limits(policy: TrafficPolicy, node_count: int) -> tuple[int, float]:
    """Return the bucket capacity and the seconds one credit takes to refill."""
    if policy != POLICY_GOVERNED:
        return RATE_LIMITER_CAPACITY, float(RATE_LIMITER_REFILL_RATE_SECONDS)

    low, high = GOVERNED_CAPACITY_RANGE
    capacity = min(
        max(GOVERNED_CAPACITY_BASE + GOVERNED_CAPACITY_PER_NODE * node_count, low), high
    )
    low, high = GOVERNED_REFILL_RANGE_PER_HOUR
    per_hour = min(max(GOVERNED_REFILL_PER_NODE_PER_HOUR * node_count, low), high)
    return capacity, SECONDS_PER_HOUR / per_hour


def request_cost(policy: TrafficPolicy, base_cost: int, *, has_path: bool = True) -> int:
    """Return what one mesh request costs; legacy charges one for everything.

    A request to a node with no established path floods the mesh, so governed
    charges it the flood cost no matter what the caller asked for.
    """
    if policy != POLICY_GOVERNED:
        return 1
    return base_cost if has_path else COST_FLOOD


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
    the poll to the moment credit returns instead.
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
    """The credit budget every mesh send crosses, sized by policy.

    Under ``legacy`` this is the historical token bucket: capacity 20, one
    token every 120 s, one token per request. Under ``governed`` capacity and
    refill scale with the number of tracked nodes, requests cost what they
    cost the mesh, and automatic traffic may not spend the reserve that keeps
    the user's own commands answerable.
    """

    def __init__(self, policy: TrafficPolicy, node_count: int = 0) -> None:
        """Size a budget for the policy and the nodes currently tracked."""
        self._policy: TrafficPolicy = policy
        capacity, refill_rate = budget_limits(policy, node_count)
        self._bucket = TokenBucket(capacity=capacity, refill_rate_seconds=refill_rate)

    @property
    def policy(self) -> TrafficPolicy:
        """Return the policy this budget is enforcing."""
        return self._policy

    def reconfigure(self, policy: TrafficPolicy, node_count: int) -> None:
        """Re-derive the limits after a settings change, keeping credit in hand."""
        capacity, refill_rate = budget_limits(policy, node_count)
        self._policy = policy
        self._bucket.capacity = capacity
        self._bucket.refill_rate = refill_rate
        self._bucket.tokens = min(self._bucket.tokens, capacity)

    def get_tokens(self) -> int:
        """Return the credits currently available (refill applied)."""
        return self._bucket.get_tokens()

    def try_consume(self, cost: int = 1, *, interactive: bool = False) -> bool:
        """Spend credits without waiting; False means the send must not happen.

        Automatic traffic may not spend below the interactive reserve, so a
        mesh saturated by polling still answers a service call.
        """
        if self._policy != POLICY_GOVERNED:
            return self._bucket.try_consume(1)
        floor = 0 if interactive else INTERACTIVE_RESERVE
        if self._bucket.get_tokens() - cost < floor:
            return False
        return self._bucket.try_consume(cost)

    def next_eligible(self, cost: int = 1, *, interactive: bool = False) -> float:
        """Return the seconds until a request of this cost could be admitted."""
        needed = cost
        if self._policy == POLICY_GOVERNED and not interactive:
            needed += INTERACTIVE_RESERVE
        elif self._policy != POLICY_GOVERNED:
            needed = 1
        missing = needed - self._bucket.get_tokens()
        return max(0.0, missing * self._bucket.refill_rate)
