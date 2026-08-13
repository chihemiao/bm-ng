import ast
from copy import deepcopy
from decimal import Decimal
from importlib import import_module
from inspect import Parameter, getsource, signature

import pytest

from data.contracts import ContractError, validate_envelope
from execution import orders
from execution.orders import OrderContractError, order_request_record


def _request(intent, recorded_ns=110):
    return order_request_record(
        intent, recorded_ns=recorded_ns, account_digest="a" * 64,
        lease_epoch=1, writer_instance_id="writer-one", wallet_fingerprint="b" * 64,
        allocated_nonce=7 if intent.leg == "hyperliquid" else None,
    )


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


@pytest.mark.parametrize(
    "changes",
    [
        {"leg": "bybit"}, {"symbol": "ETH"},
        {"side": "sell"}, {"quantity": Decimal("2")},
    ],
)
def test_serialize_rejects_an_intent_record_mismatch(changes) -> None:
    intent, _, _ = _serialized()
    fields = (
        "strategy_id", "strategy_version", "signal_ns", "leg",
        "symbol", "side", "quantity", "replacement_ordinal",
    )
    values = {field: getattr(intent, field) for field in fields}
    values.update(changes)
    wrong = _request(orders.rehydrate_order_intent(**values))
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
    assert {
        "validate_envelope", "order_request_binding_is_legacy",
        "rehydrate_order_intent", "order_request_record",
    } <= calls
    assert {"OrderIntent", "OrderRequestRecord"}.isdisjoint(calls)
    assert "hashlib" not in source and "client_order_id" not in module.__dict__


def test_order_request_rehydrator_has_one_event_parameter() -> None:
    parameters = signature(_serde().rehydrate_order_request).parameters
    assert tuple(parameters) == ("event",)
    assert parameters["event"].kind is Parameter.POSITIONAL_OR_KEYWORD


@pytest.mark.parametrize(("leg", "quantity"), [("hyperliquid", "1E+2"), ("bybit", "1.0")])
def test_order_request_event_round_trips_without_rewriting_quantity(leg, quantity) -> None:
    intent, request, event = _serialized(leg, quantity)
    before = deepcopy(event)
    restored_intent, restored_request = _serde().rehydrate_order_request(event)
    assert restored_intent == intent and restored_request == request
    assert restored_intent.quantity.as_tuple() == intent.quantity.as_tuple()
    assert event == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("strategy_id", "other"), ("strategy_version", "git-other"),
        ("signal_ns", 99), ("leg", "bybit"), ("symbol", "ETH"),
        ("side", "sell"), ("quantity", "2"), ("replacement_ordinal", 3),
    ],
)
def test_rehydrate_recomputes_cloid_for_each_bound_payload_field(field, value) -> None:
    _, _, event = _serialized()
    event["payload"][field] = value
    if field == "leg":
        event["venue"], event["payload"]["allocated_nonce"] = value, None
    with pytest.raises(OrderContractError, match="client_order_id mismatch"):
        _serde().rehydrate_order_request(event)


def test_rehydrate_rejects_a_tampered_envelope_cloid() -> None:
    _, _, event = _serialized()
    event["client_order_id"] = "0x" + "0" * 32
    with pytest.raises(OrderContractError, match="client_order_id mismatch"):
        _serde().rehydrate_order_request(event)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("account_digest", "bad", "lease binding"),
        ("allocated_nonce", None, "nonce_null"),
        ("recorded_ns", -1, "invalid_recorded_ns"),
    ],
)
def test_rehydrate_preserves_schema_contract_errors(field, value, message) -> None:
    _, _, event = _serialized()
    event["payload"][field] = value
    with pytest.raises(ContractError, match=message):
        _serde().rehydrate_order_request(event)


@pytest.mark.parametrize(
    "changes",
    [{"event_kind": "market"}, {"payload_schema": "order_observation"}],
)
def test_rehydrate_rejects_a_valid_event_of_another_kind_or_schema(changes) -> None:
    _, _, event = _serialized()
    event.update(changes)
    assert validate_envelope(event) is event
    with pytest.raises(OrderContractError, match="not an order request"):
        _serde().rehydrate_order_request(event)


def test_rehydrate_rejects_legacy_request_without_leaking_key_error() -> None:
    _, _, event = _serialized()
    event["payload"] = {"raw": "legacy"}
    del event["seq_within_boot"]
    assert validate_envelope(event) is event
    with pytest.raises(OrderContractError, match="legacy order request"):
        _serde().rehydrate_order_request(event)
