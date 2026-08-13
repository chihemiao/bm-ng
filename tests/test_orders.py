import ast
import re
import textwrap
from decimal import Decimal, localcontext
from importlib import import_module
from inspect import Parameter, getsource, signature

import pytest

from data.contracts import validate_envelope
from execution import orders
from execution.orders import (
    OrderContractError,
    ReconciliationEvidence,
    ReplayedDecisionHistory,
    decide_submission,
    make_order_intent,
    order_request_record,
    replacement_intent,
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
    }
    values.update(changes)
    return make_order_intent(**values)


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


def _serde():
    return import_module("execution.order_serde")


def _serialized(leg="hyperliquid", quantity="1E+2"):
    intent = orders.rehydrate_order_intent(
        "funding-carry", "git-deadbeef", 100, leg,
        symbol="BTC", side="buy", quantity=Decimal(quantity),
        replacement_ordinal=2,
    )
    request = order_request_record(
        intent, 110, account_digest="a" * 64, lease_epoch=1,
        writer_instance_id="writer-one", wallet_fingerprint="b" * 64,
        allocated_nonce=7 if leg == "hyperliquid" else None,
    )
    event = _serde().serialize_order_request(
        intent, request, conn_id="conn-1", boot_id="boot-1",
        recv_wall_ns=120, recv_mono_ns=90, source="execution",
        seq_within_boot=3,
    )
    return intent, request, event


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


def test_rehydrate_order_intent_exposes_the_durable_ordinal_boundary() -> None:
    parameters = signature(orders.rehydrate_order_intent).parameters
    assert tuple(parameters) == (
        "strategy_id", "strategy_version", "signal_ns", "leg",
        "symbol", "side", "quantity", "replacement_ordinal",
    )
    kinds = tuple(parameter.kind for parameter in parameters.values())
    assert kinds == (Parameter.POSITIONAL_OR_KEYWORD,) * 4 + (Parameter.KEYWORD_ONLY,) * 4
    restored = orders.rehydrate_order_intent(
        strategy_id="funding-carry", strategy_version="git-deadbeef",
        signal_ns=100, leg="hyperliquid", symbol="BTC", side="buy",
        quantity=Decimal("1E+2"), replacement_ordinal=2,
    )
    initial = _intent(quantity=Decimal("1E+2"))
    assert restored.replacement_ordinal == 2
    assert restored.client_order_id != initial.client_order_id


def test_order_creation_paths_delegate_to_the_rehydrate_constructor() -> None:
    assert "rehydrate_order_intent" in _direct_calls(make_order_intent)
    assert "rehydrate_order_intent" in _direct_calls(replacement_intent)


def test_order_request_serializer_has_one_source_for_venue() -> None:
    parameters = signature(_serde().serialize_order_request).parameters
    assert tuple(parameters) == (
        "intent", "record", "conn_id", "boot_id", "recv_wall_ns",
        "recv_mono_ns", "source", "seq_within_boot",
    )
    assert tuple(parameter.kind for parameter in parameters.values()) == (
        Parameter.POSITIONAL_OR_KEYWORD, Parameter.POSITIONAL_OR_KEYWORD,
        *(Parameter.KEYWORD_ONLY,) * 6,
    )


@pytest.mark.parametrize(("leg", "quantity"), [("hyperliquid", "1E+2"), ("bybit", "1.0")])
def test_order_request_serializer_emits_a_valid_complete_event(leg, quantity) -> None:
    intent, request, event = _serialized(leg, quantity)
    assert validate_envelope(event) is event
    assert event == {
        "schema_ver": 1, "event_kind": "order", "payload_schema": "order_request",
        "venue": leg, "conn_id": "conn-1", "boot_id": "boot-1",
        "recv_wall_ns": 120, "recv_mono_ns": 90, "source": "execution",
        "seq_within_boot": 3, "identity_status": "known",
        "client_order_id": intent.client_order_id, "venue_order_id": None,
        "payload": {
            "strategy_id": "funding-carry", "strategy_version": "git-deadbeef",
            "signal_ns": 100, "leg": leg, "symbol": "BTC", "side": "buy",
            "quantity": quantity, "replacement_ordinal": 2, "recorded_ns": 110,
            "account_digest": "a" * 64, "lease_epoch": 1,
            "writer_instance_id": "writer-one", "wallet_fingerprint": "b" * 64,
            "allocated_nonce": request.allocated_nonce,
        },
    }
    assert "client_order_id" not in event["payload"]


def test_serialize_rejects_an_intent_record_mismatch() -> None:
    intent, _, _ = _serialized()
    wrong = _request(_intent(leg="bybit"))
    with pytest.raises(OrderContractError, match="record"):
        _serde().serialize_order_request(
            intent, wrong, conn_id="conn-1", boot_id="boot-1",
            recv_wall_ns=120, recv_mono_ns=90, source="execution", seq_within_boot=3,
        )


def test_order_serde_delegates_to_contract_and_execution_constructors() -> None:
    module = _serde()
    source = getsource(module)
    tree = ast.parse(source)
    calls = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"validate_envelope", "order_request_record"} <= calls
    assert "hashlib" not in source and "client_order_id" not in module.__dict__


def test_request_record_must_exist_and_match_before_submit() -> None:
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
    assert _decide(intent, evidence, request=request) == "submit"

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


def test_only_authoritative_absence_can_reject_or_submit_a_stale_signal() -> None:
    intent = _intent()
    request = _request(intent)
    assert _decide(intent, _evidence(), request=request, now_ns=150) == "submit"
    assert _decide(intent, _evidence(), request=request, now_ns=151) == "reject_stale"


def test_replacement_requires_complete_cancelled_or_rejected_evidence() -> None:
    intent = _intent()
    replacement = replacement_intent(intent, _evidence("cancelled"), quantity=Decimal("2"))
    assert replacement.replacement_ordinal == 1
    assert replacement.client_order_id != intent.client_order_id
    assert (replacement.symbol, replacement.side) == (intent.symbol, intent.side)
    assert replacement.quantity == Decimal("2")
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
