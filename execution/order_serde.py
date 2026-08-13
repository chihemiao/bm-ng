"""Durable order-request event serialization."""

from dataclasses import fields

from data.contracts import validate_envelope
from execution.orders import (
    INTENT_FIELDS,
    OrderContractError,
    OrderIntent,
    OrderRequestRecord,
    order_request_record,
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
