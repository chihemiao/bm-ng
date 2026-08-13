"""Durable order-request event serialization."""

from dataclasses import fields
from decimal import Decimal

from data.contracts import validate_envelope
from data.schema_order_request import order_request_binding_is_legacy
from execution.orders import (
    INTENT_FIELDS,
    OrderContractError,
    OrderIntent,
    OrderRequestRecord,
    order_request_record,
    rehydrate_order_intent,
)


def serialize_order_observation(
    *, venue: str, client_order_id: str, venue_order_id: str | None,
    status: str, observation_source: str, observed_ns: int,
    venue_time_ms: int | None, conn_id: str, boot_id: str,
    recv_wall_ns: int, recv_mono_ns: int, source: str, seq_within_boot: int,
) -> dict[str, object]:
    """Build one validated durable observation with explicit envelope identity."""
    return validate_envelope(
        {
            "schema_ver": 1,
            "event_kind": "order",
            "payload_schema": "order_observation",
            "venue": venue,
            "conn_id": conn_id,
            "boot_id": boot_id,
            "recv_wall_ns": recv_wall_ns,
            "recv_mono_ns": recv_mono_ns,
            "source": source,
            "seq_within_boot": seq_within_boot,
            "identity_status": "known",
            "client_order_id": client_order_id,
            "venue_order_id": venue_order_id,
            "payload": {
                "status": status,
                "source": observation_source,
                "observed_ns": observed_ns,
                "venue_time_ms": venue_time_ms,
            },
        }
    )


def serialize_order_request(
    intent: OrderIntent,
    record: OrderRequestRecord,
    *,
    conn_id: str,
    boot_id: str,
    recv_wall_ns: int,
    recv_mono_ns: int,
    source: str,
    seq_within_boot: int,
) -> dict[str, object]:
    """Build and validate the single durable representation of an order request."""
    if not isinstance(intent, OrderIntent) or not isinstance(record, OrderRequestRecord):
        raise OrderContractError("invalid intent or record")
    intent_fields = {field: getattr(intent, field) for field in INTENT_FIELDS}
    if record.intent_fields() != intent_fields or record.client_order_id != intent.client_order_id:
        raise OrderContractError("record does not match intent")
    validated = order_request_record(
        intent,
        record.recorded_ns,
        account_digest=record.account_digest,
        lease_epoch=record.lease_epoch,
        writer_instance_id=record.writer_instance_id,
        wallet_fingerprint=record.wallet_fingerprint,
        allocated_nonce=record.allocated_nonce,
    )
    if validated != record:
        raise OrderContractError("record does not match intent")
    payload = {
        field.name: getattr(record, field.name)
        for field in fields(record)
        if field.name != "client_order_id"
    }
    payload.update(symbol=intent.symbol, side=intent.side, quantity=str(intent.quantity))
    return validate_envelope(
        {
            "schema_ver": 1,
            "event_kind": "order",
            "payload_schema": "order_request",
            "venue": intent.leg,
            "conn_id": conn_id,
            "boot_id": boot_id,
            "recv_wall_ns": recv_wall_ns,
            "recv_mono_ns": recv_mono_ns,
            "source": source,
            "seq_within_boot": seq_within_boot,
            "identity_status": "known",
            "client_order_id": intent.client_order_id,
            "venue_order_id": None,
            "payload": payload,
        }
    )


def rehydrate_order_request(
    event: dict[str, object],
) -> tuple[OrderIntent, OrderRequestRecord]:
    """Rebuild validated execution objects from one durable request event."""
    validated = validate_envelope(event)
    if validated["event_kind"] != "order" or validated["payload_schema"] != "order_request":
        raise OrderContractError("not an order request")
    payload = validated["payload"]
    if order_request_binding_is_legacy(
        payload, has_sequence="seq_within_boot" in validated,
    ):
        raise OrderContractError("legacy order request cannot be rehydrated")
    intent = rehydrate_order_intent(
        payload["strategy_id"], payload["strategy_version"],
        payload["signal_ns"], payload["leg"],
        symbol=payload["symbol"], side=payload["side"],
        quantity=Decimal(payload["quantity"]),
        replacement_ordinal=payload["replacement_ordinal"],
    )
    if intent.client_order_id != validated["client_order_id"]:
        raise OrderContractError("client_order_id mismatch")
    record = order_request_record(
        intent, payload["recorded_ns"],
        account_digest=payload["account_digest"],
        lease_epoch=payload["lease_epoch"],
        writer_instance_id=payload["writer_instance_id"],
        wallet_fingerprint=payload["wallet_fingerprint"],
        allocated_nonce=payload["allocated_nonce"],
    )
    return intent, record
