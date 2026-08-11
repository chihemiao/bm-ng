import re
from importlib import import_module

import pytest

from data.contracts import ContractError, order_request_is_legacy, validate_envelope
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


def test_client_order_id_is_cross_venue_and_binds_the_intent() -> None:
    intent = _intent()
    assert re.fullmatch(r"0x[0-9a-f]{32}", intent.client_order_id)
    assert _intent() == intent

    variants = {
        _intent(strategy_id="other").client_order_id,
        _intent(strategy_version="git-cafebabe").client_order_id,
        _intent(signal_ns=101).client_order_id,
        _intent(leg="bybit").client_order_id,
    }
    assert intent.client_order_id not in variants
    assert len(variants) == 4


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
    replacement = replacement_intent(intent, _evidence("cancelled"))
    assert replacement.replacement_ordinal == 1
    assert replacement.client_order_id != intent.client_order_id

    for status in ("pending", "unknown", "open", "partially_filled", "filled"):
        with pytest.raises(OrderContractError, match="not replaceable"):
            replacement_intent(intent, _evidence(status))
    with pytest.raises(OrderContractError, match="authoritative terminal evidence"):
        replacement_intent(intent, _evidence("rejected", fills_ns=None))


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
