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


def orders_evidence(venue, *, present=False, **changes):
    fingerprints = frozenset({"state"}) if present else frozenset()
    identities = frozenset({"identity"}) if present else frozenset()
    values = dict(
        observed_ns=100, fetched_count=int(present), page_complete=True,
        truncated=False, unknown_count=0, mismatch_count=0)
    values.update(changes)
    return SurfaceEvidence(
        **values,
        entities=CanonicalSet(f"{venue}.orders.state", 1, fingerprints),
        identities=CanonicalSet(f"{venue}.orders.identity", 1, identities))


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
    values = {
        **_META, "now_ns": 110, "max_position_age_ns": 10,
        "max_order_age_ns": 10,
        "hyperliquid_orders": orders_evidence("hyperliquid"),
        "bybit_orders": orders_evidence("bybit"), **changes}
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
    values = {
        **_META, "now_ns": 110, "max_position_age_ns": 10,
        "max_order_age_ns": 10,
        "hyperliquid_orders": orders_evidence("hyperliquid"),
        "bybit_orders": orders_evidence("bybit"), **changes}
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
    source = getsource(composition.build_kill_switch_flatten_plan)
    assert source.count("_require_authoritative_empty_orders(") == 1
    assert source.index("decision.action") < source.index(
        "_require_authoritative_empty_orders(") < source.index("build_flatten_intent_plan(")
    assert "PairCancelOutcome" not in getsource(composition)


@pytest.mark.parametrize("action", ["continue", "cancel_only_freeze"])
def test_non_flatten_decision_never_reads_positions(action):
    unreadable = _UnreadablePosition()
    assert _route(
        action, positions=(unreadable, unreadable),
        hyperliquid_orders=unreadable, bybit_orders=unreadable) is None


def test_route_rejects_a_bare_action_before_reading_positions():
    unreadable = _UnreadablePosition()
    with pytest.raises(TypeError, match="decision"):
        composition.build_kill_switch_flatten_plan(
            "flatten_and_stop",
            unreadable,
            unreadable,
            hyperliquid_orders=unreadable,
            bybit_orders=unreadable,
            **_META,
            now_ns=110,
            max_position_age_ns=10,
            max_order_age_ns=10,
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


def _bad_orders(venue, failure):
    present, changes = {
        "nonempty": (True, {}), "stale": (False, {"observed_ns": 99}),
        "truncated": (False, {"truncated": True}),
        "unknown": (False, {"fetched_count": 1, "unknown_count": 1}),
        "mismatch": (False, {"mismatch_count": 1}),
        "wrong-scheme": (False, {}),
    }[failure]
    target = ({"hyperliquid": "bybit", "bybit": "hyperliquid"}[venue]
              if failure == "wrong-scheme" else venue)
    return orders_evidence(target, present=present, **changes)


@pytest.mark.parametrize("entry", ["plan", "finalizer"])
@pytest.mark.parametrize("venue", ["hyperliquid", "bybit"])
@pytest.mark.parametrize(
    "failure", ["nonempty", "stale", "truncated", "unknown", "mismatch", "wrong-scheme"])
def test_unproven_empty_orders_precede_positions_and_never_stop(
    tmp_path, entry, venue, failure,
):
    unreadable = _UnreadablePosition()
    changes = {f"{venue}_orders": _bad_orders(venue, failure)}
    if entry == "plan":
        with pytest.raises(ValueError, match="orders"):
            _route(positions=(unreadable, unreadable), **changes)
        return
    lease, recorded = _lease(tmp_path)
    with pytest.raises(ValueError, match="orders"):
        _finalize(
            lease, _never_stop, positions=(unreadable, unreadable), **changes)
    assert lease.authority.mode == "flatten_only" and recorded == []
    lease.release()


def test_empty_orders_requirement_has_one_shared_predicate_source():
    source = getsource(composition._require_authoritative_empty_orders)
    assert source.count("state.orders_surface_confirmed_empty(") == 1
    assert "surface_is_authoritative" not in source and "fingerprints" not in source


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


def test_finalizer_delegates_real_metadata_once(tmp_path, monkeypatch):
    parameters = tuple(signature(composition.finalize_kill_switch_flatten).parameters)
    assert parameters == (
        "lease", "hyperliquid_position", "bybit_position", "hyperliquid_orders",
        "bybit_orders", "strategy_id", "strategy_version", "signal_ns", "now_ns",
        "max_position_age_ns", "max_order_age_ns", "stop")
    lease, recorded = _lease(tmp_path)
    observed, stopped, real_plan = [], [], legs.build_flatten_intent_plan

    def plan_spy(*args, **kwargs):
        observed.append(kwargs)
        return real_plan(*args, **kwargs)

    monkeypatch.setattr(legs, "build_flatten_intent_plan", plan_spy)
    authority = _finalize(lease, lambda: stopped.append(True))
    assert authority.mode == lease.authority.mode == "cancel_only"
    assert stopped == [True] and len(recorded) == 1
    assert observed == [{**_META, "now_ns": 110, "max_position_age_ns": 10}]
    source = getsource(composition.finalize_kill_switch_flatten)
    assert all(source.count(call) == 1 for call in (
        "_require_authoritative_empty_orders(", "build_flatten_intent_plan(",
        "_require_flatten_only(", "demote_kill_switch_complete("))
    assert source.index("_require_authoritative_empty_orders(") < source.index(
        "build_flatten_intent_plan(") < source.index("_require_flatten_only(")
    lease.release()


@pytest.mark.parametrize(("record_fails", "stop_fails"), [
    (True, False), (False, True), (True, True),
])
def test_finalizer_preserves_demotion_and_stop_failures(
    tmp_path, monkeypatch, record_fails, stop_fails):
    record_failure, stop_failure = OSError("record failed"), OSError("stop failed")

    def record(decision):
        if record_fails and decision.action == "demote":
            raise record_failure

    lease, _ = _lease(tmp_path, recorder=record)
    observed, stopped = [], []
    real_demote = promotion.demote_kill_switch_complete

    def capture(*args, **kwargs):
        try:
            return real_demote(*args, **kwargs)
        except WriterLeaseError as error:
            observed.append(error)
            raise

    def stop():
        stopped.append(True)
        if stop_fails:
            raise stop_failure

    monkeypatch.setattr(promotion, "demote_kill_switch_complete", capture)
    with pytest.raises(OSError if stop_fails else WriterLeaseError) as caught:
        _finalize(lease, stop)
    assert lease.authority.mode == "cancel_only" and stopped == [True]
    if record_fails:
        assert observed[0].__cause__ is record_failure
    if stop_fails:
        assert caught.value is stop_failure
        assert caught.value.__context__ is (observed[0] if record_fails else None)
    else:
        assert caught.value is observed[0]
    lease.release()
