"""Pure structural and venue semantics for durable order requests."""

from collections.abc import Mapping

from data.schema_dispatch import (
    ORDER_BOUND_FIELDS,
    ORDER_LEASE_FIELDS,
    ORDER_LEGS,
    ORDER_SIDES,
    ORDER_SYMBOLS,
)


def _presence(
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


def order_request_binding_is_legacy(
    payload: Mapping[str, object], *, has_sequence: object
) -> bool:
    legacy, _ = _presence(payload, has_sequence, ORDER_BOUND_FIELDS)
    return legacy


def order_request_lease_binding_errors(
    payload: Mapping[str, object], *, venue: object, has_sequence: object
) -> tuple[str, ...]:
    fields = ORDER_LEASE_FIELDS + ("allocated_nonce",)
    legacy, missing = _presence(payload, has_sequence, fields)
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
    legacy, missing = _presence(payload, has_sequence, ORDER_BOUND_FIELDS)
    if legacy:
        return ()
    if missing:
        return (f"order_request:partial_binding:{','.join(missing)}",)
    errors = order_request_lease_binding_errors(
        payload, venue=venue, has_sequence=has_sequence,
    )
    if errors:
        return errors
    for field, allowed in (
        ("symbol", ORDER_SYMBOLS), ("side", ORDER_SIDES), ("leg", ORDER_LEGS),
    ):
        value = payload[field]
        if not isinstance(value, str) or value not in allowed:
            return (f"order_request:invalid_{field}",)
    leg = payload["leg"]
    if leg != venue:
        return (f"order_request:leg_venue_mismatch:{leg}:{venue}",)
    return ()
