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


@pytest.mark.parametrize("node_count", [0, 1, 4, 8, 18, 40])
def test_legacy_budget_limits_never_move(node_count: int) -> None:
    assert traffic.budget_limits(LEGACY, node_count) == (20, 120.0)


@pytest.mark.parametrize(
    ("node_count", "capacity", "per_hour"),
    [(1, 20, 24), (4, 20, 24), (8, 28, 48), (18, 48, 96), (40, 48, 96)],
)
def test_governed_budget_scales_with_tracked_nodes(
    node_count: int, capacity: int, per_hour: int
) -> None:
    assert traffic.budget_limits(GOVERNED, node_count) == (capacity, 3600 / per_hour)


def test_legacy_charges_one_credit_for_every_request() -> None:
    assert traffic.request_cost(LEGACY, traffic.COST_FLOOD) == 1
    assert traffic.request_cost(LEGACY, traffic.COST_DIRECT, has_path=False) == 1


def test_governed_charges_flood_for_an_unknown_route() -> None:
    assert traffic.request_cost(GOVERNED, traffic.COST_DIRECT) == 1
    assert traffic.request_cost(GOVERNED, traffic.COST_LOGIN_STATUS) == 2
    assert traffic.request_cost(GOVERNED, traffic.COST_DIRECT, has_path=False) == 8


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


def test_legacy_budget_spends_one_credit_per_request(clock) -> None:
    budget = traffic.MeshBudget(LEGACY, node_count=8)
    assert budget.get_tokens() == 20

    for _ in range(20):
        assert budget.try_consume(traffic.COST_FLOOD) is True
    assert budget.get_tokens() == 0
    assert budget.try_consume(1) is False
    assert budget.next_eligible(1) == 120.0

    clock.value += 120
    assert budget.try_consume(1, interactive=True) is True


def test_governed_budget_keeps_a_reserve_for_interactive_sends(clock) -> None:
    budget = traffic.MeshBudget(GOVERNED, node_count=4)
    assert budget.get_tokens() == 20

    assert budget.try_consume(traffic.COST_FLOOD) is True  # 12 left
    assert budget.try_consume(traffic.COST_FLOOD) is False  # would leave 4, below reserve
    assert budget.get_tokens() == 12

    assert budget.try_consume(traffic.COST_FLOOD, interactive=True) is True
    assert budget.get_tokens() == 4
    assert budget.try_consume(1) is False
    assert budget.try_consume(1, interactive=True) is True


def test_governed_next_eligible_counts_the_reserve(clock) -> None:
    budget = traffic.MeshBudget(GOVERNED, node_count=4)
    while budget.try_consume(1, interactive=True):
        pass
    assert budget.get_tokens() == 0
    assert budget.next_eligible(1, interactive=True) == 150.0
    assert budget.next_eligible(1) == 7 * 150.0

    clock.value += 150 * 7
    assert budget.try_consume(1) is True


def test_reconfigure_resizes_without_handing_out_credit(clock) -> None:
    budget = traffic.MeshBudget(GOVERNED, node_count=18)
    assert budget.get_tokens() == 48

    budget.reconfigure(GOVERNED, 1)
    assert budget.get_tokens() == 20

    budget.reconfigure(LEGACY, 1)
    assert budget.policy == LEGACY
    assert traffic.budget_limits(LEGACY, 1) == (20, 120.0)
    assert budget.try_consume(traffic.COST_FLOOD) is True
    assert budget.get_tokens() == 19
