"""Pure structural and venue semantics for durable order requests."""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from data.schema_dispatch import (
    ORDER_BOUND_FIELDS,
    ORDER_LEASE_FIELDS,
    ORDER_LEGS,
    ORDER_SIDES,
    ORDER_SYMBOLS,
)


def binding_presence(
    payload: Mapping[str, object], has_sequence: object, fields: tuple[str, ...],
) -> tuple[bool, tuple[str, ...]]:
    if type(has_sequence) is not bool:
        raise TypeError("has_sequence must be bool")
    present = tuple(field in payload for field in fields)
    if not has_sequence and not any(present):
        return True, ()
    missing = [
        field for field, exists in zip(fields, present, strict=True) if not exists
    ]
    if not has_sequence:
        missing.append("sequence")
    return False, tuple(sorted(missing))


def _valid_digest(value: object) -> bool:
    is_hex = isinstance(value, str) and all(char in "0123456789abcdef" for char in value)
    return is_hex and len(value) == 64


def _valid_lease(payload: Mapping[str, object]) -> bool:
    account, epoch, instance, wallet = (payload[field] for field in ORDER_LEASE_FIELDS)
    valid = _valid_digest(account) and type(epoch) is int and epoch > 0
    return valid and isinstance(instance, str) and bool(instance) and _valid_digest(wallet)


def _valid_ns(value: object) -> bool:
    return type(value) is int and value >= 0


def _valid_quantity(value: object) -> bool:
    if not isinstance(value, str) or not value or value.strip() != value:
        return False
    try:
        quantity = Decimal(value)
    except InvalidOperation:
        return False
    return quantity.is_finite() and quantity > 0


def _order_value_errors(payload: Mapping[str, object]) -> tuple[str, ...]:
    signal_valid = _valid_ns(payload["signal_ns"])
    recorded_valid = _valid_ns(payload["recorded_ns"])
    checks = (
        (
            "strategy_id",
            isinstance(payload["strategy_id"], str) and bool(payload["strategy_id"]),
        ),
        (
            "strategy_version",
            isinstance(payload["strategy_version"], str)
            and bool(payload["strategy_version"]),
        ),
        ("signal_ns", signal_valid),
        ("leg", isinstance(payload["leg"], str) and payload["leg"] in ORDER_LEGS),
        ("symbol", isinstance(payload["symbol"], str) and payload["symbol"] in ORDER_SYMBOLS),
        ("side", isinstance(payload["side"], str) and payload["side"] in ORDER_SIDES),
        ("replacement_ordinal", _valid_ns(payload["replacement_ordinal"])),
        ("quantity", _valid_quantity(payload["quantity"])),
        ("recorded_ns", recorded_valid),
    )
    errors = [f"order_request:invalid_{field}" for field, valid in checks if not valid]
    if signal_valid and recorded_valid and payload["recorded_ns"] < payload["signal_ns"]:
        errors.append("order_request:invalid_recorded_ns")
    return tuple(errors)


def order_request_binding_is_legacy(
    payload: Mapping[str, object], *, has_sequence: object
) -> bool:
    legacy, _ = binding_presence(payload, has_sequence, ORDER_BOUND_FIELDS)
    return legacy


def order_request_lease_binding_errors(
    payload: Mapping[str, object], *, venue: object, has_sequence: object
) -> tuple[str, ...]:
    fields = ORDER_LEASE_FIELDS + ("allocated_nonce",)
    legacy, missing = binding_presence(payload, has_sequence, fields)
    if legacy:
        return ()
    if missing:
        return (f"order_request:partial_binding:{','.join(missing)}",)
    if venue not in {"hyperliquid", "bybit"}:
        return (f"order_request:unknown_venue:{venue}",)
    if not _valid_lease(payload):
        return ("invalid order request lease binding",)
    nonce = payload["allocated_nonce"]
    if venue == "hyperliquid" and nonce is None:
        return ("order_request:hyperliquid_nonce_null",)
    if venue == "hyperliquid" and (type(nonce) is not int or nonce <= 0):
        return ("order_request:hyperliquid_nonce_invalid",)
    if venue == "bybit" and nonce is not None:
        return ("order_request:bybit_nonce_not_null",)
    return ()


def order_request_binding_errors(
    payload: Mapping[str, object], *, venue: object, has_sequence: object
) -> tuple[str, ...]:
    legacy, missing = binding_presence(payload, has_sequence, ORDER_BOUND_FIELDS)
    if legacy:
        return ()
    if missing:
        return (f"order_request:partial_binding:{','.join(missing)}",)
    errors = order_request_lease_binding_errors(
        payload, venue=venue, has_sequence=has_sequence,
    )
    if errors:
        return errors
    errors = list(_order_value_errors(payload))
    leg = payload["leg"]
    if leg in ORDER_LEGS and leg != venue:
        errors.append(f"order_request:leg_venue_mismatch:{leg}:{venue}")
    return tuple(errors)


def order_request_event_binding(
    event: Mapping[str, object],
) -> tuple[bool, tuple[str, ...]]:
    payload = event["payload"]
    has_sequence = "seq_within_boot" in event
    return order_request_binding_is_legacy(
        payload, has_sequence=has_sequence,
    ), order_request_binding_errors(
        payload, venue=event.get("venue"), has_sequence=has_sequence,
    )
