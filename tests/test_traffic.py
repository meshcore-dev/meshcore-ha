"""Traffic policy decisions: the frozen legacy numbers and the governed ones.

``traffic.py`` is pure Python, so it is loaded from source (with its real
constants and token bucket) rather than through the package stub the unit tier
installs. The per-node timeline these helpers drive is pinned end to end in
``tests_integration/test_traffic_timeline.py``, which needs a real coordinator.
"""

import time
from types import SimpleNamespace

import pytest

from tests.support.modules import load_module

load_module("const")
RATE_LIMITER = load_module("rate_limiter")
traffic = load_module("traffic")

LEGACY = traffic.POLICY_LEGACY
GOVERNED = traffic.POLICY_GOVERNED


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    """A monotonic clock the test advances by hand."""
    state = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(
        RATE_LIMITER, "time", SimpleNamespace(monotonic=lambda: state.value, time=time.time)
    )
    return state


def _entry(options=None, data=None) -> SimpleNamespace:
    """A config entry stand-in exposing only what policy resolution reads."""
    return SimpleNamespace(options=options or {}, data=data or {})


def test_policy_defaults_to_legacy_and_reads_options_before_data() -> None:
    assert traffic.resolve_policy(_entry()) == LEGACY
    assert traffic.resolve_policy(_entry(data={"traffic_policy": "governed"})) == GOVERNED
    assert (
        traffic.resolve_policy(
            _entry(options={"traffic_policy": "legacy"}, data={"traffic_policy": "governed"})
        )
        == LEGACY
    )
    assert traffic.resolve_policy(_entry(data={"traffic_policy": "nonsense"})) == LEGACY


@pytest.mark.parametrize(
    ("failures", "delay"),
    [(0, 116), (1, 232), (2, 464), (3, 928), (4, 1856), (5, 3712), (6, 7200), (9, 7200)],
)
def test_legacy_backoff_ladder(failures: int, delay: int) -> None:
    assert traffic.backoff_delay(LEGACY, failures, 7200) == delay


def test_legacy_backoff_floor_for_tiny_intervals() -> None:
    assert traffic.backoff_delay(LEGACY, 0, 30) == 1
    assert traffic.backoff_delay(LEGACY, 3, 30) == 8


@pytest.mark.parametrize("failures", [0, 1, 2, 5, 12])
def test_governed_backoff_is_jittered_and_capped(failures: int) -> None:
    delay = traffic.backoff_delay(GOVERNED, failures, 7200)
    expected = min(7200 * 2**failures, traffic.GOVERNED_BACKOFF_CAP_SECONDS)
    assert 0.9 * expected <= delay <= 1.1 * expected
    assert delay <= traffic.GOVERNED_BACKOFF_CAP_SECONDS * 1.1


@pytest.mark.parametrize("failures", [0, 1, 2, 5, 12])
def test_governed_routed_backoff_uses_the_legacy_ladder(failures: int) -> None:
    assert traffic.backoff_delay(GOVERNED, failures, 7200, routed=True) == (
        traffic.backoff_delay(LEGACY, failures, 7200)
    )


def test_routed_does_not_move_legacy() -> None:
    assert traffic.backoff_delay(LEGACY, 3, 7200, routed=True) == 928


def test_only_governed_heals_a_reset_path() -> None:
    assert traffic.heals_path(GOVERNED) is True
    assert traffic.heals_path(LEGACY) is False


@pytest.mark.parametrize("policy", [LEGACY, GOVERNED])
def test_login_needs_five_failures_and_an_hour_of_cooldown(policy: str) -> None:
    assert traffic.should_login(policy, 4, 0, 100_000) is False
    assert traffic.should_login(policy, 5, 0, 100_000) is True
    assert traffic.should_login(policy, 5, 100_000 - 3599, 100_000) is False
    assert traffic.should_login(policy, 5, 100_000 - 3600, 100_000) is True


@pytest.mark.parametrize("policy", [LEGACY, GOVERNED])
def test_path_reset_needs_three_failures_a_path_and_no_toggle(policy: str) -> None:
    assert traffic.should_reset_path(policy, 2, True, False) is False
    assert traffic.should_reset_path(policy, 3, True, False) is True
    assert traffic.should_reset_path(policy, 3, False, False) is False
    assert traffic.should_reset_path(policy, 9, True, True) is False


def test_denial_is_a_failure_only_under_legacy() -> None:
    assert traffic.denial_counts_as_failure(LEGACY) is True
    assert traffic.denial_counts_as_failure(GOVERNED) is False


def test_legacy_auto_disable_reaches_repeaters_in_the_status_loop_only() -> None:
    assert traffic.auto_disable_applies(LEGACY, traffic.NODE_REPEATER) is True
    assert (
        traffic.auto_disable_applies(LEGACY, traffic.NODE_REPEATER, telemetry=True) is False
    )
    assert traffic.auto_disable_applies(LEGACY, traffic.NODE_CLIENT) is False
    assert traffic.auto_disable_applies(LEGACY, traffic.NODE_CLIENT, telemetry=True) is False


@pytest.mark.parametrize("node_type", [traffic.NODE_REPEATER, traffic.NODE_CLIENT])
@pytest.mark.parametrize("telemetry", [False, True])
def test_governed_auto_disable_covers_both_types_in_both_loops(
    node_type: str, telemetry: bool
) -> None:
    assert traffic.auto_disable_applies(GOVERNED, node_type, telemetry=telemetry) is True




ROUTED = {"out_path_len": 2}
UNROUTED = {"out_path_len": -1}


@pytest.mark.parametrize("op", [traffic.OP_STATUS, traffic.OP_TELEMETRY, traffic.OP_LOGIN])
def test_a_routed_contact_is_direct_and_an_unrouted_one_floods(op: str) -> None:
    assert traffic.classify_lane(op, ROUTED) == traffic.LANE_DIRECT
    assert traffic.classify_lane(op, UNROUTED) == traffic.LANE_FLOOD
    assert traffic.classify_lane(op, {}) == traffic.LANE_FLOOD
    assert traffic.classify_lane(op, None) == traffic.LANE_FLOOD


def test_mesh_wide_operations_flood_whatever_the_route() -> None:
    assert traffic.classify_lane(traffic.OP_ADVERT, ROUTED) == traffic.LANE_FLOOD
    assert traffic.classify_lane(traffic.OP_PATH_DISCOVERY, ROUTED) == traffic.LANE_FLOOD


def test_the_request_after_a_path_reset_floods() -> None:
    assert (
        traffic.classify_lane(traffic.OP_STATUS, ROUTED, path_reset=True) == traffic.LANE_FLOOD
    )
    assert traffic.classify_lane(traffic.OP_STATUS, ROUTED) == traffic.LANE_DIRECT


@pytest.mark.parametrize(
    "op", [traffic.OP_MESSAGE, traffic.OP_CHANNEL_MESSAGE, traffic.OP_TRACE]
)
@pytest.mark.parametrize("contact", [ROUTED, UNROUTED, None])
def test_user_messages_have_their_own_lane(op: str, contact: dict | None) -> None:
    assert traffic.classify_lane(op, contact) == traffic.LANE_MESSAGES


def test_governed_lane_rates_are_flat_and_do_not_move() -> None:
    assert traffic.GOVERNED_LANES == {
        traffic.LANE_FLOOD: (5, 20),
        traffic.LANE_DIRECT: (20, 120),
        traffic.LANE_MESSAGES: (10, 60),
    }


def test_legacy_pools_every_lane_into_one_bucket(clock) -> None:
    budget = traffic.MeshBudget(LEGACY)
    assert budget.get_tokens() == 20

    for lane in traffic.GOVERNED_LANES:
        assert budget.try_consume(lane) is True
    assert budget.get_tokens() == 17

    while budget.try_consume(traffic.LANE_DIRECT):
        pass
    assert budget.get_tokens() == 0
    assert budget.try_consume(traffic.LANE_MESSAGES) is False
    assert budget.next_eligible(traffic.LANE_FLOOD) == 120.0

    clock.value += 120
    assert budget.try_consume(traffic.LANE_FLOOD) is True


def test_governed_lanes_are_independent(clock) -> None:
    budget = traffic.MeshBudget(GOVERNED)

    for _ in range(5):
        assert budget.try_consume(traffic.LANE_FLOOD) is True
    assert budget.try_consume(traffic.LANE_FLOOD) is False
    assert budget.next_eligible(traffic.LANE_FLOOD) == 180.0

    assert budget.try_consume(traffic.LANE_DIRECT) is True
    assert budget.try_consume(traffic.LANE_MESSAGES) is True
    assert budget.next_eligible(traffic.LANE_DIRECT) == 0.0

    clock.value += 180
    assert budget.try_consume(traffic.LANE_FLOOD) is True


def test_governed_sensor_state_reports_direct_credits(clock) -> None:
    budget = traffic.MeshBudget(GOVERNED)
    assert budget.get_tokens() == 20
    assert budget.try_consume(traffic.LANE_DIRECT) is True
    assert budget.get_tokens() == 19
    assert budget.try_consume(traffic.LANE_FLOOD) is True
    assert budget.get_tokens() == 19


def test_governed_attributes_expose_every_lane_rate(clock) -> None:
    budget = traffic.MeshBudget(GOVERNED)
    for _ in range(5):
        budget.try_consume(traffic.LANE_FLOOD)

    attributes = budget.attributes()
    assert attributes is not None
    assert attributes["policy"] == GOVERNED
    assert attributes["flood_capacity"] == 5
    assert attributes["flood_refill_per_hour"] == 20
    assert attributes["flood_credits"] == 0
    assert attributes["flood_next_eligible"].endswith("+00:00")
    assert attributes["direct_credits"] == 20
    assert attributes["direct_next_eligible"] is None
    assert attributes["messages_capacity"] == 10
    assert attributes["messages_refill_per_hour"] == 60


def test_legacy_publishes_no_lane_attributes(clock) -> None:
    assert traffic.MeshBudget(LEGACY).attributes() is None


def test_governed_credits_survive_a_restart(clock) -> None:
    budget = traffic.MeshBudget(GOVERNED)
    for _ in range(5):
        budget.try_consume(traffic.LANE_FLOOD)
    stored = budget.snapshot()
    assert stored["lanes"] == {"flood": 0, "direct": 20, "messages": 10}

    restored = traffic.MeshBudget(GOVERNED)
    restored.restore({**stored, "at": stored["at"] - 390})  # two credits back
    assert restored.try_consume(traffic.LANE_FLOOD) is True
    assert restored.try_consume(traffic.LANE_FLOOD) is True
    assert restored.try_consume(traffic.LANE_FLOOD) is False


def test_legacy_persists_no_credits(clock) -> None:
    budget = traffic.MeshBudget(LEGACY)
    assert budget.snapshot() == {}
    budget.restore({"at": 0, "lanes": {"flood": 3}})
    assert budget.get_tokens() == 20
