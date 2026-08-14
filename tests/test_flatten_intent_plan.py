from dataclasses import replace
from decimal import Decimal
from inspect import getsource, signature

import pytest

import execution.orders as orders
import reconciliation.kill_switch_composition as composition
import reconciliation.legs as legs
import reconciliation.promotion as promotion
from execution.writer import WriterIdentity, WriterLease, WriterLeaseError
from reconciliation.exposure import LegPosition
from reconciliation.kill_switch import KillSwitchDecision
from reconciliation.state import CanonicalSet, SurfaceEvidence

_STATE = CanonicalSet("positions.state", 1, frozenset({"state"}))
_IDENTITY = CanonicalSet("positions.identity", 1, frozenset({"identity"}))
_EVIDENCE = SurfaceEvidence(
    observed_ns=100, fetched_count=1, page_complete=True, truncated=False,
    unknown_count=0, mismatch_count=0, entities=_STATE, identities=_IDENTITY,
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
        **_META, leg=leg, symbol="BTC", side="buy",
        quantity=Decimal("1"), reduce_only=reduce_only,
    )


def _plan(**changes):
    return orders.FlattenIntentPlan(
        **{**_META, "hyperliquid": None, "bybit": None, **changes}
    )


def _lease(root, mode="flatten_only", recorder=None):
    recorded = []
    lease = WriterLease.acquire(
        root, WriterIdentity("test-account", "writer-one", "b" * 64, "boot-one"),
        recorded.append if recorder is None else recorder, acquired_ns=90)
    lease._authority = lease.authority._replace(mode=mode)
    recorded.clear()
    return lease, recorded


def _finalize(lease, stop, *, positions=None, **changes):
    values = {**_META, "now_ns": 110, "max_position_age_ns": 10, **changes}
    pair = positions or (_leg("hyperliquid", "0"), _leg("bybit", "0"))
    return composition.finalize_kill_switch_flatten(lease, *pair, stop=stop, **values)


def _never_stop():
    raise AssertionError("stop must not be called")


def test_each_nonzero_position_gets_an_independent_reduce_only_intent():
    plan = _build()
    assert (plan.hyperliquid.leg, plan.hyperliquid.side, plan.hyperliquid.quantity) == (
        "hyperliquid", "buy", Decimal("2"),
    )
    assert (plan.bybit.leg, plan.bybit.side, plan.bybit.quantity) == (
        "bybit", "sell", Decimal("1"),
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


def test_authoritative_flat_finalizes_authority_then_stops(tmp_path):
    lease, stopped = _lease(tmp_path), []
    authority = _finalize(lease[0], lambda: stopped.append(True))
    assert authority.mode == lease[0].authority.mode == "cancel_only"
    assert stopped == [True] and len(lease[1]) == 1
    lease[0].release()


@pytest.mark.parametrize("positions", [
    (_leg("hyperliquid", "1"), _leg("bybit", "0")),
    (_leg("hyperliquid", "0"), _leg("bybit", "-1")),
    (replace(_leg("hyperliquid", "0"), evidence=replace(_EVIDENCE, observed_ns=99)),
     _leg("bybit", "0")),
    (replace(_leg("hyperliquid", "0"), evidence=replace(_EVIDENCE, truncated=True)),
     _leg("bybit", "0")),
    (replace(_leg("hyperliquid", "0"), evidence=replace(
        _EVIDENCE, fetched_count=2, unknown_count=1)), _leg("bybit", "0")),
    (_leg("bybit", "0"), _leg("hyperliquid", "0")),
    (_leg("hyperliquid", "0"), replace(_leg("bybit", "0"), symbol="ETH")),
], ids=("hl-nonzero", "bybit-nonzero", "stale", "truncated", "unknown", "venue", "symbol"))
def test_unproven_flatness_never_demotes_or_stops(tmp_path, positions):
    lease, recorded = _lease(tmp_path)
    with pytest.raises(ValueError):
        _finalize(lease, _never_stop, positions=positions)
    assert lease.authority.mode == "flatten_only" and recorded == []
    lease.release()


@pytest.mark.parametrize("mode", ["pending_reconciliation", "cancel_only", "risk_increasing"])
def test_wrong_entry_mode_never_stops(tmp_path, mode):
    lease, recorded = _lease(tmp_path, mode)
    with pytest.raises(WriterLeaseError, match="flatten only"):
        _finalize(lease, _never_stop)
    assert lease.authority.mode == mode and recorded == []
    lease.release()


def test_finalizer_signature_requires_raw_positions_not_a_plan():
    parameters = tuple(signature(composition.finalize_kill_switch_flatten).parameters)
    assert parameters == (
        "lease", "hyperliquid_position", "bybit_position", "strategy_id",
        "strategy_version", "signal_ns", "now_ns", "max_position_age_ns", "stop")
    assert "plan" not in parameters


def test_finalizer_delegates_real_metadata_once(tmp_path, monkeypatch):
    lease, _ = _lease(tmp_path)
    observed, real_plan = [], legs.build_flatten_intent_plan
    real_demote = promotion.demote_kill_switch_complete

    def plan_spy(*args, **kwargs):
        observed.append(("plan", kwargs))
        return real_plan(*args, **kwargs)

    def demote_spy(*args, **kwargs):
        observed.append(("demote", kwargs))
        return real_demote(*args, **kwargs)

    monkeypatch.setattr(legs, "build_flatten_intent_plan", plan_spy)
    monkeypatch.setattr(promotion, "demote_kill_switch_complete", demote_spy)
    _finalize(lease, lambda: None)
    assert observed == [
        ("plan", {**_META, "now_ns": 110, "max_position_age_ns": 10}),
        ("demote", {"now_ns": 110}),
    ]
    source = getsource(composition.finalize_kill_switch_flatten)
    assert all(source.count(call) == 1 for call in (
        "build_flatten_intent_plan(", "_require_flatten_only(",
        "demote_kill_switch_complete("))
    lease.release()


def _record_failure_lease(root):
    failure = OSError("decision stream unavailable")

    def fail(decision):
        if decision.action == "demote":
            raise failure

    return _lease(root, recorder=fail)[0], failure


def test_record_failure_still_stops_and_propagates(tmp_path):
    lease, failure = _record_failure_lease(tmp_path)
    stopped = []
    with pytest.raises(WriterLeaseError) as caught:
        _finalize(lease, lambda: stopped.append(True))
    assert caught.value.__cause__ is failure
    assert lease.authority.mode == "cancel_only" and stopped == [True]
    lease.release()


def test_stop_failure_wins_after_successful_demotion(tmp_path):
    lease, _ = _lease(tmp_path)
    failure = OSError("stop failed")
    with pytest.raises(OSError) as caught:
        _finalize(lease, lambda: (_ for _ in ()).throw(failure))
    assert caught.value is failure and lease.authority.mode == "cancel_only"
    lease.release()


def test_stop_failure_keeps_demotion_failure_as_context(tmp_path, monkeypatch):
    lease, record_failure = _record_failure_lease(tmp_path)
    stop_failure, observed = OSError("stop failed"), []
    real_demote = promotion.demote_kill_switch_complete

    def capture(*args, **kwargs):
        try:
            return real_demote(*args, **kwargs)
        except WriterLeaseError as error:
            observed.append(error)
            raise

    monkeypatch.setattr(promotion, "demote_kill_switch_complete", capture)
    with pytest.raises(OSError) as caught:
        _finalize(lease, lambda: (_ for _ in ()).throw(stop_failure))
    assert caught.value is stop_failure and caught.value.__context__ is observed[0]
    assert observed[0].__cause__ is record_failure
    lease.release()
