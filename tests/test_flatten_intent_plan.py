from dataclasses import replace
from decimal import Decimal

import pytest

import execution.orders as orders
import reconciliation.kill_switch_composition as composition
import reconciliation.legs as legs
from reconciliation.exposure import LegPosition
from reconciliation.kill_switch import KillSwitchDecision
from reconciliation.state import CanonicalSet, SurfaceEvidence

_STATE = CanonicalSet("positions.state", 1, frozenset({"state"}))
_IDENTITY = CanonicalSet("positions.identity", 1, frozenset({"identity"}))
_EVIDENCE = SurfaceEvidence(
    observed_ns=100,
    fetched_count=1,
    page_complete=True,
    truncated=False,
    unknown_count=0,
    mismatch_count=0,
    entities=_STATE,
    identities=_IDENTITY,
)
_META = {"strategy_id": "funding-carry", "strategy_version": "git-deadbeef", "signal_ns": 0}


def _leg(venue, quantity, evidence=_EVIDENCE):
    return LegPosition(venue, "BTC", Decimal(quantity), evidence)


def _build(hl="-2", bybit="1", *, positions=None, **changes):
    values = {**_META, "now_ns": 110, "max_position_age_ns": 10, **changes}
    pair = positions or (_leg("hyperliquid", hl), _leg("bybit", bybit))
    return legs.build_flatten_intent_plan(*pair, **values)


def _intent(leg="hyperliquid", *, reduce_only=True):
    return orders.make_order_intent(
        **_META,
        leg=leg,
        symbol="BTC",
        side="buy",
        quantity=Decimal("1"),
        reduce_only=reduce_only,
    )


def _plan(**changes):
    return orders.FlattenIntentPlan(**{**_META, "hyperliquid": None, "bybit": None, **changes})


def test_each_nonzero_position_gets_an_independent_reduce_only_intent():
    plan = _build()
    assert (plan.hyperliquid.leg, plan.hyperliquid.side, plan.hyperliquid.quantity) == (
        "hyperliquid",
        "buy",
        Decimal("2"),
    )
    assert (plan.bybit.leg, plan.bybit.side, plan.bybit.quantity) == (
        "bybit",
        "sell",
        Decimal("1"),
    )


@pytest.mark.parametrize(
    ("hl", "bybit", "present"), [("-2", "0", "hyperliquid"), ("0", "1", "bybit")]
)
def test_zero_position_omits_only_that_venue_intent(hl, bybit, present):
    plan = _build(hl, bybit)
    absent = "bybit" if present == "hyperliquid" else "hyperliquid"
    assert getattr(plan, present) is not None and getattr(plan, absent) is None


def test_authoritative_zero_positions_form_a_valid_empty_plan():
    plan = _build("0", "0")
    assert (plan.hyperliquid, plan.bybit) == (None, None)
    assert (plan.strategy_id, plan.strategy_version, plan.signal_ns) == tuple(_META.values())


def test_generated_intents_share_only_the_plan_metadata():
    plan = _build()
    for intent in (plan.hyperliquid, plan.bybit):
        assert (intent.strategy_id, intent.strategy_version, intent.signal_ns) == tuple(
            _META.values()
        )
    assert plan.hyperliquid.side != plan.bybit.side
    assert plan.hyperliquid.quantity != plan.bybit.quantity


@pytest.mark.parametrize("index", [0, 1])
def test_any_non_authoritative_position_rejects_the_whole_plan(index):
    pair = [_leg("hyperliquid", "-1"), _leg("bybit", "1")]
    pair[index] = replace(pair[index], evidence=replace(_EVIDENCE, truncated=True))
    with pytest.raises(ValueError, match="authoritative"):
        _build(positions=pair)


def test_named_venue_slots_cannot_be_swapped():
    with pytest.raises(ValueError, match="venue"):
        _build(positions=(_leg("bybit", "1"), _leg("hyperliquid", "-1")))


@pytest.mark.parametrize(
    "pair", [(object(), _leg("bybit", "1")), (_leg("hyperliquid", "-1"), object())]
)
def test_builder_rejects_non_position_inputs(pair):
    with pytest.raises(TypeError, match="LegPosition"):
        _build(positions=pair)


@pytest.mark.parametrize(
    "changes",
    [{"strategy_id": ""}, {"strategy_version": ""}, {"signal_ns": True}, {"signal_ns": -1}],
)
def test_empty_plan_still_validates_plan_metadata(changes):
    with pytest.raises(ValueError):
        _plan(**changes)


@pytest.mark.parametrize(
    ("changes", "error", "match"),
    [
        ({"hyperliquid": object()}, TypeError, "OrderIntent"),
        ({"hyperliquid": _intent("bybit")}, ValueError, "leg"),
        ({"hyperliquid": _intent(reduce_only=False)}, ValueError, "reduce_only"),
    ],
)
def test_plan_rejects_invalid_slot_values(changes, error, match):
    with pytest.raises(error, match=match):
        _plan(**changes)


@pytest.mark.parametrize(
    ("field", "value"),
    [("strategy_id", "other"), ("strategy_version", "git-cafebabe"), ("signal_ns", 1)],
)
def test_plan_rejects_intent_metadata_divergence(field, value):
    with pytest.raises(ValueError, match="metadata"):
        _plan(hyperliquid=replace(_intent(), **{field: value}))


class _UnreadablePosition:
    def __getattribute__(self, name):
        raise AssertionError(f"non-flatten action read position attribute {name}")

    def __eq__(self, other):
        raise AssertionError(f"non-flatten action compared position with {other!r}")


def _route(action="flatten_and_stop", *, positions=None, **changes):
    values = {**_META, "now_ns": 110, "max_position_age_ns": 10, **changes}
    selected = positions or (_leg("hyperliquid", "-2"), _leg("bybit", "1"))
    return composition.build_kill_switch_flatten_plan(
        KillSwitchDecision(action), *selected, **values
    )


def test_flatten_decision_routes_every_planner_input():
    plan = _route()
    assert (plan.strategy_id, plan.strategy_version, plan.signal_ns) == tuple(_META.values())
    assert (plan.hyperliquid.side, plan.hyperliquid.quantity) == ("buy", Decimal("2"))
    assert (plan.bybit.side, plan.bybit.quantity) == ("sell", Decimal("1"))
    assert plan.hyperliquid.reduce_only and plan.bybit.reduce_only


@pytest.mark.parametrize("action", ["continue", "cancel_only_freeze"])
def test_non_flatten_decision_never_reads_positions(action):
    unreadable = _UnreadablePosition()
    assert _route(action, positions=(unreadable, unreadable)) is None


def test_route_rejects_a_bare_action_before_reading_positions():
    unreadable = _UnreadablePosition()
    with pytest.raises(TypeError, match="decision"):
        composition.build_kill_switch_flatten_plan(
            "flatten_and_stop",
            unreadable,
            unreadable,
            **_META,
            now_ns=110,
            max_position_age_ns=10,
        )


@pytest.mark.parametrize(
    ("positions", "error", "match"),
    [
        ((_leg("bybit", "1"), _leg("hyperliquid", "-1")), ValueError, "venue"),
        ((object(), _leg("bybit", "1")), TypeError, "LegPosition"),
        (
            (
                replace(_leg("hyperliquid", "-1"), evidence=replace(_EVIDENCE, truncated=True)),
                _leg("bybit", "1"),
            ),
            ValueError,
            "authoritative",
        ),
    ],
)
def test_flatten_decision_preserves_planner_fail_closed_checks(positions, error, match):
    with pytest.raises(error, match=match):
        _route(positions=positions)
