from decimal import Decimal
from importlib import import_module

import pytest

from data.contracts import ContractError, order_request_is_legacy, validate_envelope
from execution.orders import OrderContractError, make_order_intent, order_request_record


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


def _bound_event(venue="hyperliquid", *, allocated_nonce=7, sequence=True, **changes):
    payload = {
        "account_digest": "a" * 64,
        "lease_epoch": 1,
        "writer_instance_id": "writer-one",
        "wallet_fingerprint": "b" * 64,
        "allocated_nonce": allocated_nonce,
    }
    payload.update(changes)
    event = {
        "schema_ver": 1,
        "event_kind": "order",
        "payload_schema": "order_request",
        "venue": venue,
        "conn_id": "conn-1",
        "boot_id": "boot-1",
        "recv_wall_ns": 1_000,
        "recv_mono_ns": 900,
        "source": "test",
        "payload": payload,
        "identity_status": "known",
        "client_order_id": "0xrequest",
        "venue_order_id": None,
    }
    if sequence:
        event["seq_within_boot"] = 9
    return event


def test_request_record_binds_the_writer_lease_snapshot() -> None:
    request = order_request_record(
        _intent(), recorded_ns=110, account_digest="a" * 64,
        lease_epoch=3, writer_instance_id="writer-one", wallet_fingerprint="b" * 64,
        allocated_nonce=7,
    )

    assert request.account_digest == "a" * 64
    assert request.lease_epoch == 3
    assert request.writer_instance_id == "writer-one"
    assert request.wallet_fingerprint == "b" * 64
    assert request.allocated_nonce == 7


def test_request_record_rejects_a_truncated_wallet_fingerprint() -> None:
    with pytest.raises(OrderContractError, match="wallet_fingerprint"):
        order_request_record(
            _intent(), recorded_ns=110, account_digest="a" * 64,
            lease_epoch=1, writer_instance_id="writer-one", wallet_fingerprint="b" * 32,
            allocated_nonce=7,
        )


@pytest.mark.parametrize(
    ("venue", "allocated_nonce"), [("hyperliquid", 7), ("bybit", None)]
)
def test_order_request_binds_the_venue_signer_semantics(venue, allocated_nonce) -> None:
    intent = _intent(leg=venue)
    assert order_request_record(
        intent, recorded_ns=110, account_digest="a" * 64,
        lease_epoch=1, writer_instance_id="writer-one", wallet_fingerprint="b" * 64,
        allocated_nonce=allocated_nonce,
    ).allocated_nonce == allocated_nonce
    assert validate_envelope(_bound_event(venue, allocated_nonce=allocated_nonce))


@pytest.mark.parametrize(
    ("venue", "allocated_nonce", "error"),
    [
        ("hyperliquid", None, "order_request:hyperliquid_nonce_null"),
        ("bybit", 7, "order_request:bybit_nonce_not_null"),
    ],
)
def test_order_request_rejects_wrong_venue_nonce_semantics(
    venue, allocated_nonce, error
) -> None:
    with pytest.raises(OrderContractError) as execution_error:
        order_request_record(
            _intent(leg=venue), recorded_ns=110, account_digest="a" * 64,
            lease_epoch=1, writer_instance_id="writer-one", wallet_fingerprint="b" * 64,
            allocated_nonce=allocated_nonce,
        )
    assert str(execution_error.value) == error
    with pytest.raises(ContractError) as contract_error:
        validate_envelope(_bound_event(venue, allocated_nonce=allocated_nonce))
    assert str(contract_error.value) == error


def test_four_field_order_request_is_partial_not_legacy() -> None:
    event = _bound_event()
    event["payload"].pop("allocated_nonce")
    expected = "order_request:partial_binding:allocated_nonce"
    with pytest.raises(ContractError) as error:
        order_request_is_legacy(event)
    assert str(error.value) == expected


def test_unbound_order_request_remains_legacy() -> None:
    event = _bound_event(sequence=False)
    for field in tuple(event["payload"]):
        event["payload"].pop(field)
    assert order_request_is_legacy(event)
    assert validate_envelope(event) is event


def test_order_request_record_requires_an_allocated_nonce_argument() -> None:
    with pytest.raises(TypeError, match="allocated_nonce"):
        order_request_record(
            _intent(), recorded_ns=110, account_digest="a" * 64,
            lease_epoch=1, writer_instance_id="writer-one", wallet_fingerprint="b" * 64,
        )


def test_nonce_presence_does_not_weaken_lease_binding_atomicity() -> None:
    event = _bound_event()
    event["payload"].pop("wallet_fingerprint")
    with pytest.raises(ContractError) as error:
        validate_envelope(event)
    assert str(error.value) == "order_request:partial_binding:wallet_fingerprint"


def test_payload_leg_cannot_disagree_with_the_envelope() -> None:
    with pytest.raises(ContractError) as error:
        validate_envelope(_bound_event(leg="bybit"))
    assert str(error.value) == "order_request:leg_venue_mismatch:bybit:hyperliquid"


def test_unknown_venue_does_not_emit_a_secondary_nonce_error() -> None:
    schema = import_module("data.schema_order_request")
    event = _bound_event("unknown", allocated_nonce=None)
    errors = schema.order_request_binding_errors(
        event["payload"], venue=event["venue"], has_sequence=True
    )
    assert errors == ("order_request:unknown_venue:unknown",)


def test_order_request_sequence_presence_requires_an_exact_boolean() -> None:
    schema = import_module("data.schema_order_request")
    with pytest.raises(TypeError, match="has_sequence must be bool"):
        schema.order_request_binding_errors(
            _bound_event()["payload"], venue="hyperliquid", has_sequence=1
        )


@pytest.mark.parametrize("allocated_nonce", [0, -1, True, "7", 7.0])
def test_schema_rejects_invalid_hyperliquid_nonce_domain(allocated_nonce) -> None:
    schema = import_module("data.schema_order_request")
    errors = schema.order_request_binding_errors(
        _bound_event(allocated_nonce=allocated_nonce)["payload"],
        venue="hyperliquid",
        has_sequence=True,
    )
    assert errors == ("order_request:hyperliquid_nonce_invalid",)


@pytest.mark.parametrize("allocated_nonce", [0, -1, True, "7", 7.0])
def test_execution_reuses_schema_rule_for_invalid_hyperliquid_nonce(
    allocated_nonce,
) -> None:
    orders = import_module("execution.orders")
    schema = import_module("data.schema_order_request")
    assert orders.order_request_binding_errors is schema.order_request_binding_errors
    with pytest.raises(OrderContractError) as error:
        order_request_record(
            _intent(), recorded_ns=110, account_digest="a" * 64,
            lease_epoch=1, writer_instance_id="writer-one", wallet_fingerprint="b" * 64,
            allocated_nonce=allocated_nonce,
        )
    assert str(error.value) == "order_request:hyperliquid_nonce_invalid"


def test_bound_order_request_requires_a_durable_sequence() -> None:
    with pytest.raises(ContractError) as error:
        validate_envelope(_bound_event(sequence=False))
    assert str(error.value) == "order_request:partial_binding:sequence"
