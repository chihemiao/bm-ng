from dataclasses import replace
from decimal import Decimal

import pytest

import execution.orders as orders
import reconciliation.legs as legs
from reconciliation.exposure import LegPosition
from reconciliation.state import CanonicalSet, SurfaceEvidence


def _canonical(kind: str) -> CanonicalSet:
    return CanonicalSet(f"positions.{kind}", 1, frozenset({kind}))


def _evidence(**changes) -> SurfaceEvidence:
    values = {
        "observed_ns": 100,
        "fetched_count": 1,
        "page_complete": True,
        "truncated": False,
        "unknown_count": 0,
        "mismatch_count": 0,
        "entities": _canonical("state"),
        "identities": _canonical("identity"),
    }
    values.update(changes)
    return SurfaceEvidence(**values)


def _leg(venue: str, quantity: str, **changes) -> LegPosition:
    values = {
        "venue": venue,
        "symbol": "BTC",
        "signed_quantity": Decimal(quantity),
        "evidence": _evidence(),
    }
    values.update(changes)
    return LegPosition(**values)


def _build(
    hl: str = "-2",
    bybit: str = "1",
    *,
    hyperliquid_position=None,
    bybit_position=None,
    **changes,
):
    values = {
        "strategy_id": "funding-carry",
        "strategy_version": "git-deadbeef",
        "signal_ns": 0,
        "now_ns": 110,
        "max_position_age_ns": 10,
    }
    values.update(changes)
    return legs.build_flatten_intent_plan(
        hyperliquid_position or _leg("hyperliquid", hl),
        bybit_position or _leg("bybit", bybit),
        **values,
    )


def _intent(leg: str, **changes):
    values = {
        "strategy_id": "funding-carry",
        "strategy_version": "git-deadbeef",
        "signal_ns": 0,
        "leg": leg,
        "symbol": "BTC",
        "side": "buy",
        "quantity": Decimal("1"),
        "reduce_only": True,
    }
    values.update(changes)
    return orders.make_order_intent(**values)


def _plan(**changes):
    values = {
        "strategy_id": "funding-carry",
        "strategy_version": "git-deadbeef",
        "signal_ns": 0,
        "hyperliquid": None,
        "bybit": None,
    }
    values.update(changes)
    return orders.FlattenIntentPlan(**values)


def test_each_nonzero_position_gets_an_independent_reduce_only_intent():
    plan = _build()
    assert plan.hyperliquid is not None and plan.bybit is not None
    assert (plan.hyperliquid.leg, plan.hyperliquid.side) == ("hyperliquid", "buy")
    assert (plan.bybit.leg, plan.bybit.side) == ("bybit", "sell")
    assert (plan.hyperliquid.quantity, plan.bybit.quantity) == (
        Decimal("2"),
        Decimal("1"),
    )


@pytest.mark.parametrize(
    ("hl", "bybit", "present"),
    [("-2", "0", "hyperliquid"), ("0", "1", "bybit")],
)
def test_zero_position_omits_only_that_venue_intent(hl, bybit, present):
    plan = _build(hl, bybit)
    absent = "bybit" if present == "hyperliquid" else "hyperliquid"
    assert getattr(plan, present) is not None
    assert getattr(plan, absent) is None


def test_authoritative_zero_positions_form_a_valid_empty_plan():
    plan = _build("0", "0")
    assert (plan.hyperliquid, plan.bybit) == (None, None)
    assert (plan.strategy_id, plan.strategy_version, plan.signal_ns) == (
        "funding-carry",
        "git-deadbeef",
        0,
    )


def test_generated_intents_share_only_the_plan_metadata():
    plan = _build()
    for intent in (plan.hyperliquid, plan.bybit):
        assert intent is not None
        assert (intent.strategy_id, intent.strategy_version, intent.signal_ns) == (
            plan.strategy_id,
            plan.strategy_version,
            plan.signal_ns,
        )
    assert plan.hyperliquid.side != plan.bybit.side
    assert plan.hyperliquid.quantity != plan.bybit.quantity


@pytest.mark.parametrize("venue", ["hyperliquid", "bybit"])
def test_any_non_authoritative_position_rejects_the_whole_plan(venue):
    positions = {
        "hyperliquid": _leg("hyperliquid", "-1"),
        "bybit": _leg("bybit", "1"),
    }
    positions[venue] = replace(positions[venue], evidence=_evidence(truncated=True))
    with pytest.raises(ValueError, match="authoritative"):
        _build(
            hyperliquid_position=positions["hyperliquid"],
            bybit_position=positions["bybit"],
        )


def test_named_venue_slots_cannot_be_swapped():
    with pytest.raises(ValueError, match="venue"):
        _build(
            hyperliquid_position=_leg("bybit", "1"),
            bybit_position=_leg("hyperliquid", "-1"),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"strategy_id": ""},
        {"strategy_version": ""},
        {"signal_ns": True},
        {"signal_ns": -1},
    ],
)
def test_empty_plan_still_validates_plan_metadata(changes):
    with pytest.raises(ValueError):
        _plan(**changes)


def test_plan_rejects_non_intent_slot_values():
    with pytest.raises(TypeError, match="OrderIntent"):
        _plan(hyperliquid=object())


def test_plan_rejects_an_intent_in_the_wrong_venue_slot():
    with pytest.raises(ValueError, match="leg"):
        _plan(hyperliquid=_intent("bybit"))


def test_plan_rejects_a_non_reducing_intent():
    with pytest.raises(ValueError, match="reduce_only"):
        _plan(hyperliquid=_intent("hyperliquid", reduce_only=False))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("strategy_id", "other"),
        ("strategy_version", "git-cafebabe"),
        ("signal_ns", 1),
    ],
)
def test_plan_rejects_intent_metadata_divergence(field, value):
    intent = replace(_intent("hyperliquid"), **{field: value})
    with pytest.raises(ValueError, match="metadata"):
        _plan(hyperliquid=intent)
