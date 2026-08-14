import ast
import re
import textwrap
from dataclasses import replace
from decimal import Decimal, localcontext
from inspect import Parameter, getsource, signature

import pytest

from execution import orders
from execution.orders import (
    OrderContractError,
    ReconciliationEvidence,
    ReplayedDecisionHistory,
    T0APairIntents,
    decide_submission,
    make_order_intent,
    make_t0a_pair_intents,
    order_request_record,
    replacement_intent,
    t0a_pair_intents_match,
)


def _intent(**changes):
    values = {
        "strategy_id": "funding-carry",
        "strategy_version": "git-deadbeef",
        "signal_ns": 100,
        "leg": "hyperliquid",
        "symbol": "BTC",
        "side": "buy",
        "quantity": Decimal("1"),
        "reduce_only": False,
    }
    values.update(changes)
    return make_order_intent(**values)


def _t0a(**changes):
    values = {
        "strategy_id": "funding-carry",
        "strategy_version": "git-deadbeef",
        "signal_ns": 100,
        "symbol": "BTC",
        "quantity": Decimal("1"),
    }
    values.update(changes)
    return make_t0a_pair_intents(**values)


def _evidence(status="absent", **changes):
    values = {
        "status": status,
        "orders_ns": 101,
        "fills_ns": 102,
        "positions_ns": 103,
    }
    values.update(changes)
    return ReconciliationEvidence(**values)


def _history(intent, *, attempts=0, frozen=False):
    return ReplayedDecisionHistory(intent.client_order_id, attempts, frozen)


def _request(intent, recorded_ns=110):
    return order_request_record(
        intent, recorded_ns=recorded_ns, account_digest="a" * 64,
        lease_epoch=1, writer_instance_id="writer-one", wallet_fingerprint="b" * 64,
        allocated_nonce=7 if intent.leg == "hyperliquid" else None,
    )


def _decide(intent, evidence, *, request=None, history=None, now_ns=120):
    return decide_submission(
        intent,
        evidence,
        request,
        history or _history(intent),
        now_ns=now_ns,
        max_signal_age_ns=50,
        max_reconcile_attempts=3,
    )


def _direct_calls(function) -> set[str]:
    source = textwrap.dedent(getsource(function))
    return {
        node.func.id for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_client_order_id_is_cross_venue_and_binds_the_intent() -> None:
    intent = _intent()
    assert re.fullmatch(r"0x[0-9a-f]{32}", intent.client_order_id)
    assert _intent() == intent
    assert (intent.symbol, intent.side, intent.quantity) == ("BTC", "buy", Decimal("1"))

    variants = {
        _intent(strategy_id="other").client_order_id,
        _intent(strategy_version="git-cafebabe").client_order_id,
        _intent(signal_ns=101).client_order_id,
        _intent(leg="bybit").client_order_id,
        _intent(symbol="ETH").client_order_id,
        _intent(side="sell").client_order_id,
        _intent(quantity=Decimal("2")).client_order_id,
    }
    assert intent.client_order_id not in variants
    assert len(variants) == 7


def test_order_quantity_identity_is_numeric_not_representational() -> None:
    ids = {_intent(quantity=Decimal(value)).client_order_id for value in ("1", "1.0", "1E0")}
    assert ids == {_intent().client_order_id}


def test_order_quantity_identity_is_decimal_context_independent() -> None:
    quantity = Decimal("123456789.123456789")
    expected = _intent(quantity=quantity).client_order_id
    with localcontext() as context:
        context.prec = 5
        assert _intent(quantity=quantity).client_order_id == expected


def test_extreme_exponent_identity_is_bounded_and_numeric() -> None:
    values = ("1E999999999", "10E999999998")
    ids = {_intent(quantity=Decimal(value)).client_order_id for value in values}
    assert len(ids) == 1 and re.fullmatch(r"0x[0-9a-f]{32}", ids.pop())


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"symbol": "SOL"}, ValueError), ({"symbol": 1}, TypeError),
        ({"side": "long"}, ValueError), ({"side": 1}, TypeError),
        ({"quantity": 1}, TypeError), ({"quantity": Decimal("NaN")}, ValueError),
        ({"quantity": Decimal("Infinity")}, ValueError),
        ({"quantity": Decimal("0")}, ValueError), ({"quantity": Decimal("-1")}, ValueError),
    ],
)
def test_order_intent_rejects_invalid_trade_terms(changes, error) -> None:
    with pytest.raises(error):
        _intent(**changes)


def test_new_order_trade_terms_are_required() -> None:
    parameters = signature(make_order_intent).parameters
    names = ("symbol", "side", "quantity")
    assert all(parameters[name].default is Parameter.empty for name in names)


def test_reduce_only_is_a_required_keyword_only_intent_term() -> None:
    for constructor in (make_order_intent, orders.rehydrate_order_intent):
        parameter = signature(constructor).parameters["reduce_only"]
        assert parameter.kind is Parameter.KEYWORD_ONLY
        assert parameter.default is Parameter.empty


@pytest.mark.parametrize("reduce_only", [False, True])
def test_reduce_only_accepts_only_explicit_boolean_semantics(reduce_only) -> None:
    assert _intent(reduce_only=reduce_only).reduce_only is reduce_only


@pytest.mark.parametrize("reduce_only", [0, 1, None])
def test_reduce_only_rejects_non_boolean_values(reduce_only) -> None:
    with pytest.raises(TypeError, match="reduce_only"):
        _intent(reduce_only=reduce_only)


def test_reduce_only_changes_the_client_order_identity() -> None:
    ordinary = _intent(reduce_only=False)
    reducing = _intent(reduce_only=True)
    assert ordinary.client_order_id != reducing.client_order_id


def test_rehydrate_order_intent_exposes_the_durable_ordinal_boundary() -> None:
    parameters = signature(orders.rehydrate_order_intent).parameters
    assert tuple(parameters) == (
        "strategy_id", "strategy_version", "signal_ns", "leg",
        "symbol", "side", "quantity", "reduce_only", "replacement_ordinal",
    )
    kinds = tuple(parameter.kind for parameter in parameters.values())
    assert kinds == (Parameter.POSITIONAL_OR_KEYWORD,) * 4 + (Parameter.KEYWORD_ONLY,) * 5
    restored = orders.rehydrate_order_intent(
        strategy_id="funding-carry", strategy_version="git-deadbeef",
        signal_ns=100, leg="hyperliquid", symbol="BTC", side="buy",
        quantity=Decimal("1E+2"), reduce_only=False, replacement_ordinal=2,
    )
    initial = _intent(quantity=Decimal("1E+2"))
    assert restored.replacement_ordinal == 2
    assert restored.client_order_id != initial.client_order_id


def test_order_creation_paths_delegate_to_the_rehydrate_constructor() -> None:
    assert "rehydrate_order_intent" in _direct_calls(make_order_intent)
    assert "rehydrate_order_intent" in _direct_calls(replacement_intent)


def test_t0a_pair_intents_freeze_the_two_named_equal_quantity_legs() -> None:
    pair = _t0a()
    assert isinstance(pair, T0APairIntents) and not hasattr(pair, "__dict__")
    assert pair.hyperliquid == make_order_intent(
        "funding-carry", "git-deadbeef", 100, "hyperliquid",
        symbol="BTC", side="sell", quantity=Decimal("1"), reduce_only=False,
    )
    assert pair.bybit == make_order_intent(
        "funding-carry", "git-deadbeef", 100, "bybit",
        symbol="BTC", side="buy", quantity=Decimal("1"), reduce_only=False,
    )
    assert pair.hyperliquid.client_order_id != pair.bybit.client_order_id
    with pytest.raises(AttributeError):
        pair.bybit = pair.hyperliquid


def test_t0a_pair_creator_exposes_only_the_frozen_topology_inputs() -> None:
    parameters = signature(make_t0a_pair_intents).parameters
    assert tuple(parameters) == (
        "strategy_id", "strategy_version", "signal_ns", "symbol", "quantity",
    )
    kinds = tuple(parameter.kind for parameter in parameters.values())
    assert kinds == (Parameter.POSITIONAL_OR_KEYWORD,) * 3 + (Parameter.KEYWORD_ONLY,) * 2
    assert all(parameter.default is Parameter.empty for parameter in parameters.values())
    assert all(parameter.kind is Parameter.KEYWORD_ONLY
               for parameter in signature(T0APairIntents).parameters.values())
    assert "make_order_intent" in _direct_calls(make_t0a_pair_intents)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"strategy_id": ""}, ValueError), ({"signal_ns": True}, ValueError),
        ({"symbol": "SOL"}, ValueError), ({"symbol": 1}, TypeError),
        ({"quantity": Decimal("0")}, ValueError), ({"quantity": 1}, TypeError),
    ],
)
def test_t0a_pair_creator_delegates_invalid_inputs(changes, error) -> None:
    with pytest.raises(error):
        _t0a(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"strategy_id": "other"}, {"strategy_version": "git-other"},
        {"signal_ns": 101}, {"symbol": "ETH"}, {"quantity": Decimal("2")},
    ],
)
def test_t0a_pair_match_rejects_different_pairing_fields(changes) -> None:
    base, other = _t0a(), _t0a(**changes)
    assert not t0a_pair_intents_match(replace(base, bybit=other.bybit))


def test_t0a_pair_match_freezes_topology_but_not_independent_replacement() -> None:
    pair = _t0a()
    assert t0a_pair_intents_match(pair)
    swapped = replace(
        pair,
        hyperliquid=make_order_intent(
            "funding-carry", "git-deadbeef", 100, "hyperliquid",
            symbol="BTC", side="buy", quantity=Decimal("1"), reduce_only=False,
        ),
        bybit=make_order_intent(
            "funding-carry", "git-deadbeef", 100, "bybit",
            symbol="BTC", side="sell", quantity=Decimal("1"), reduce_only=False,
        ),
    )
    assert not t0a_pair_intents_match(swapped)
    wrong_legs = T0APairIntents(
        hyperliquid=replace(pair.hyperliquid, leg="bybit"),
        bybit=replace(pair.bybit, leg="hyperliquid"),
    )
    assert not t0a_pair_intents_match(wrong_legs)
    reducing = T0APairIntents(
        hyperliquid=_intent(side="sell", reduce_only=True),
        bybit=_intent(leg="bybit", side="buy", reduce_only=True),
    )
    assert not t0a_pair_intents_match(reducing)
    replaced = replacement_intent(
        pair.bybit, _evidence("cancelled"), quantity=pair.bybit.quantity,
    )
    assert replaced.replacement_ordinal == 1
    assert t0a_pair_intents_match(replace(pair, bybit=replaced))


@pytest.mark.parametrize("value", [None, (), object()])
def test_t0a_pair_match_rejects_non_pair_types(value) -> None:
    with pytest.raises(TypeError, match="pair must be T0APairIntents"):
        t0a_pair_intents_match(value)


def test_request_needs_post_record_absence_and_must_match_before_submit() -> None:
    intent = _intent()
    evidence = _evidence()
    assert _decide(intent, evidence) == "persist"

    request = _request(intent)
    assert request.client_order_id == intent.client_order_id
    assert request.intent_fields() == {
        "strategy_id": "funding-carry",
        "strategy_version": "git-deadbeef",
        "signal_ns": 100,
        "leg": "hyperliquid",
        "replacement_ordinal": 0,
    }
    assert _decide(intent, evidence, request=request) == "reconcile"
    post_request = _evidence(orders_ns=111, fills_ns=112, positions_ns=113)
    assert _decide(intent, post_request, request=request) == "submit"
    equal_request = _request(intent, recorded_ns=111)
    equal_time = _evidence(orders_ns=111, fills_ns=111, positions_ns=111)
    assert _decide(intent, equal_time, request=equal_request) == "reconcile"

    wrong_request = _request(_intent(leg="bybit"))
    with pytest.raises(OrderContractError, match="request does not match intent"):
        _decide(intent, evidence, request=wrong_request)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"orders_ns": None}, "query failed"),
        ({"fills_ns": 100}, "not later than signal"),
        ({"positions_ns": 99}, "older than signal"),
    ],
)
def test_absence_needs_three_successful_post_signal_queries(changes, reason) -> None:
    del reason
    intent = _intent()
    assert _decide(intent, _evidence(**changes)) == "reconcile"


@pytest.mark.parametrize("status", ["pending", "unknown"])
def test_ambiguous_state_reconciles_before_staleness_and_then_freezes(status) -> None:
    intent = _intent()
    stale_now = 1_000
    assert _decide(intent, _evidence(status), now_ns=stale_now) == "reconcile"
    exhausted = _history(intent, attempts=3)
    assert _decide(intent, _evidence(status), history=exhausted, now_ns=stale_now) == "freeze"


def test_replayed_freeze_is_an_absorbing_state() -> None:
    intent = _intent()
    request = _request(intent)
    frozen = _history(intent, frozen=True)
    assert _decide(intent, _evidence(), request=request, history=frozen) == "freeze"


@pytest.mark.parametrize(
    "status", ["open", "partially_filled", "filled", "cancelled", "rejected"]
)
def test_any_known_order_state_holds_the_same_intent(status) -> None:
    intent = _intent()
    assert _decide(intent, _evidence(status), now_ns=1_000) == "hold"


def test_only_post_request_absence_can_submit_or_reject_a_stale_signal() -> None:
    intent = _intent()
    request = _request(intent)
    evidence = _evidence(orders_ns=111, fills_ns=112, positions_ns=113)
    assert _decide(intent, evidence, request=request, now_ns=150) == "submit"
    assert _decide(intent, evidence, request=request, now_ns=151) == "reject_stale"


def test_replacement_requires_complete_cancelled_or_rejected_evidence() -> None:
    intent = _intent(reduce_only=True)
    replacement = replacement_intent(intent, _evidence("cancelled"), quantity=Decimal("2"))
    assert replacement.replacement_ordinal == 1
    assert replacement.client_order_id != intent.client_order_id
    assert (replacement.symbol, replacement.side) == (intent.symbol, intent.side)
    assert replacement.quantity == Decimal("2")
    assert replacement.reduce_only
    parameters = signature(replacement_intent).parameters
    assert set(parameters) == {"previous", "evidence", "quantity"}
    assert parameters["quantity"].kind is Parameter.KEYWORD_ONLY
    assert parameters["quantity"].default is Parameter.empty
    with pytest.raises(TypeError):
        replacement_intent(intent, _evidence("cancelled"))

    for status in ("pending", "unknown", "open", "partially_filled", "filled"):
        with pytest.raises(OrderContractError, match="not replaceable"):
            replacement_intent(intent, _evidence(status), quantity=Decimal("1"))
    with pytest.raises(OrderContractError, match="authoritative terminal evidence"):
        replacement_intent(intent, _evidence("rejected", fills_ns=None), quantity=Decimal("1"))


def test_structural_invalidity_fails_closed() -> None:
    intent = _intent()
    with pytest.raises(OrderContractError, match="clock moved backwards"):
        _decide(intent, _evidence(), now_ns=99)
    with pytest.raises(OrderContractError, match="history does not match intent"):
        _decide(intent, _evidence(), history=_history(_intent(leg="bybit")))
    with pytest.raises(OrderContractError, match="invalid max_reconcile_attempts"):
        decide_submission(intent, _evidence(), None, _history(intent), 120, 50, 0)
    with pytest.raises(OrderContractError, match="invalid strategy_id"):
        _intent(strategy_id="")
