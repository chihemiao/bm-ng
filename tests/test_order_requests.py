from decimal import Decimal
from importlib import import_module

import pytest

from data.contracts import ContractError, order_request_is_legacy, validate_envelope
from execution.orders import OrderContractError, make_order_intent, order_request_record

REQUEST_FIELDS = (
    "account_digest", "lease_epoch", "writer_instance_id", "wallet_fingerprint",
    "allocated_nonce", "strategy_id", "strategy_version", "signal_ns", "leg",
    "replacement_ordinal", "symbol", "side", "quantity", "recorded_ns",
)
NEW_REQUEST_FIELDS = REQUEST_FIELDS[5:]


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
        "strategy_id": "funding-carry",
        "strategy_version": "git-deadbeef",
        "signal_ns": 100,
        "leg": venue,
        "replacement_ordinal": 0,
        "symbol": "BTC",
        "side": "buy",
        "quantity": "1",
        "recorded_ns": 110,
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


def test_partial_order_request_names_missing_allocated_nonce() -> None:
    event = _bound_event()
    event["payload"].pop("allocated_nonce")
    expected = "order_request:partial_binding:allocated_nonce"
    with pytest.raises(ContractError) as error:
        order_request_is_legacy(event)
    assert str(error.value) == expected


def test_old_five_field_bound_request_is_partial_not_legacy() -> None:
    event = _bound_event()
    for field in NEW_REQUEST_FIELDS:
        event["payload"].pop(field)
    schema = import_module("data.schema_order_request")
    assert schema.order_request_lease_binding_errors(
        event["payload"], venue=event["venue"], has_sequence=True,
    ) == ()
    expected = f"order_request:partial_binding:{','.join(sorted(NEW_REQUEST_FIELDS))}"
    with pytest.raises(ContractError) as error:
        order_request_is_legacy(event)
    assert str(error.value) == expected


@pytest.mark.parametrize("missing", REQUEST_FIELDS)
def test_bound_request_fields_are_one_atomic_presence_group(missing) -> None:
    event = _bound_event()
    event["payload"].pop(missing)
    with pytest.raises(ContractError) as error:
        validate_envelope(event)
    assert str(error.value) == f"order_request:partial_binding:{missing}"


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
    assert orders.order_request_lease_binding_errors is schema.order_request_lease_binding_errors
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", "SOL"), ("symbol", 1),
        ("side", "long"), ("side", 1),
        ("leg", "unknown"), ("leg", 1),
    ],
)
def test_order_request_rejects_invalid_closed_trade_terms(field, value) -> None:
    event = _bound_event(**{field: value})
    with pytest.raises(ContractError) as error:
        validate_envelope(event)
    assert str(error.value) == f"order_request:invalid_{field}"


def test_order_request_closed_sets_have_one_identity() -> None:
    dispatch = import_module("data.schema_dispatch")
    schema = import_module("data.schema_order_request")
    orders = import_module("execution.orders")
    for name in ("ORDER_SYMBOLS", "ORDER_SIDES", "ORDER_LEGS"):
        assert getattr(schema, name) is getattr(dispatch, name)
        assert getattr(orders, name) is getattr(dispatch, name)


@pytest.mark.parametrize(
    ("field", "value"),
    [("quantity", "abc"), ("signal_ns", -5), ("strategy_id", "")],
)
def test_order_request_rejects_the_b1a_known_value_gaps(field, value) -> None:
    with pytest.raises(ContractError) as error:
        validate_envelope(_bound_event(**{field: value}))
    assert str(error.value) == f"order_request:invalid_{field}"


@pytest.mark.parametrize("field", ["strategy_id", "strategy_version"])
@pytest.mark.parametrize("value", ["", 1, None])
def test_order_request_rejects_invalid_strategy_identifiers(field, value) -> None:
    with pytest.raises(ContractError) as error:
        validate_envelope(_bound_event(**{field: value}))
    assert str(error.value) == f"order_request:invalid_{field}"


@pytest.mark.parametrize("field", ["signal_ns", "replacement_ordinal", "recorded_ns"])
@pytest.mark.parametrize("value", [-1, True, "1", 1.0])
def test_order_request_rejects_invalid_integer_domains(field, value) -> None:
    changes = {field: value}
    if field == "recorded_ns":
        changes["signal_ns"] = 0
    with pytest.raises(ContractError) as error:
        validate_envelope(_bound_event(**changes))
    assert str(error.value) == f"order_request:invalid_{field}"


@pytest.mark.parametrize(
    "quantity",
    [
        "", " ", "abc", "NaN", "sNaN", "Infinity", "-Infinity", "0", "-1",
        " 1 ", "1 ", " 1", 1, Decimal("1"),
    ],
)
def test_order_request_rejects_invalid_quantity_strings(quantity) -> None:
    with pytest.raises(ContractError) as error:
        validate_envelope(_bound_event(quantity=quantity))
    assert str(error.value) == "order_request:invalid_quantity"


@pytest.mark.parametrize("quantity", ["1", "1.0", "1E+2", "0.0001"])
def test_order_request_accepts_exact_positive_quantity_strings(quantity) -> None:
    assert validate_envelope(_bound_event(quantity=quantity))


@pytest.mark.parametrize(
    ("signal_ns", "accepted"), [(-1, False), (True, False), (0, True)]
)
def test_signal_time_boundary_stays_aligned_with_execution(signal_ns, accepted) -> None:
    try:
        _intent(signal_ns=signal_ns)
    except OrderContractError:
        execution_accepted = False
    else:
        execution_accepted = True
    try:
        validate_envelope(_bound_event(signal_ns=signal_ns))
    except ContractError:
        durable_accepted = False
    else:
        durable_accepted = True
    assert execution_accepted is durable_accepted is accepted


def test_zero_signal_and_recorded_times_are_valid_together() -> None:
    assert validate_envelope(_bound_event(signal_ns=0, recorded_ns=0))


def test_recorded_time_cannot_precede_signal_time() -> None:
    with pytest.raises(ContractError) as error:
        validate_envelope(_bound_event(signal_ns=1, recorded_ns=0))
    assert str(error.value) == "order_request:invalid_recorded_ns"


def test_time_relation_needs_two_individually_valid_operands() -> None:
    schema = import_module("data.schema_order_request")
    event = _bound_event(signal_ns="1", recorded_ns=0)
    assert schema.order_request_binding_errors(
        event["payload"], venue=event["venue"], has_sequence=True,
    ) == ("order_request:invalid_signal_ns",)


@pytest.mark.parametrize("field", ["strategy_id", "strategy_version"])
def test_strategy_whitespace_policy_stays_aligned_with_execution(field) -> None:
    assert _intent(**{field: " "})
    assert validate_envelope(_bound_event(**{field: " "}))


def test_value_rules_do_not_leak_into_the_execution_lease_primitive() -> None:
    schema = import_module("data.schema_order_request")
    event = _bound_event(
        strategy_id="", signal_ns=True, replacement_ordinal=-1,
        quantity="abc", recorded_ns="110",
    )
    assert schema.order_request_lease_binding_errors(
        event["payload"], venue=event["venue"], has_sequence=True,
    ) == ()


def test_order_request_value_errors_accumulate_in_field_order() -> None:
    schema = import_module("data.schema_order_request")
    event = _bound_event(
        strategy_id="", strategy_version=1, signal_ns=True,
        leg="unknown", symbol="SOL", side="long", replacement_ordinal=-1,
        quantity=" 1 ", recorded_ns="110",
    )
    assert schema.order_request_binding_errors(
        event["payload"], venue=event["venue"], has_sequence=True,
    ) == (
        "order_request:invalid_strategy_id",
        "order_request:invalid_strategy_version",
        "order_request:invalid_signal_ns",
        "order_request:invalid_leg",
        "order_request:invalid_symbol",
        "order_request:invalid_side",
        "order_request:invalid_replacement_ordinal",
        "order_request:invalid_quantity",
        "order_request:invalid_recorded_ns",
    )
