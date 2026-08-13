"""Observation semantics on inputs whose common envelope fields were prevalidated."""

from collections.abc import Mapping

from data.schema_dispatch import ORDER_LEGS, ORDER_STATUSES
from data.schema_order_request import _presence

FIELDS = ("status", "source", "observed_ns", "venue_time_ms")
# Venue stays in the envelope: unlike a request leg, it is not identity/preimage data.
SOURCES = frozenset(
    {"submission_response", "order_status", "execution_history", "no_venue_response"}
)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _ns(value: object) -> bool:
    return type(value) is int and value >= 0


def order_observation_binding_is_legacy(
    payload: Mapping[str, object], *, has_sequence: object) -> bool:
    return _presence(payload, has_sequence, FIELDS)[0]


def order_observation_binding_errors(
    payload: Mapping[str, object], *, venue: object, has_sequence: object,
    identity_status: object, client_order_id: object, recv_wall_ns: object,
) -> tuple[str, ...]:
    legacy, missing = _presence(payload, has_sequence, FIELDS)
    if legacy:
        return ()
    if missing:
        return (f"order_observation:partial_binding:{','.join(missing)}",)
    status, source = payload["status"], payload["source"]
    observed, venue_time = payload["observed_ns"], payload["venue_time_ms"]
    observed_valid, time_valid = _ns(observed), venue_time is None or _ns(venue_time)
    checks = (
        ("invalid_fields", set(payload) == set(FIELDS)),
        ("invalid_status", isinstance(status, str) and status in ORDER_STATUSES),
        ("invalid_source", isinstance(source, str) and source in SOURCES),
        ("invalid_observed_ns", observed_valid),
        ("invalid_venue_time_ms", time_valid),
        (f"unknown_venue:{venue}", venue in ORDER_LEGS),
        ("identity_not_known", identity_status == "known"),
        ("invalid_client_order_id", _text(client_order_id)),
    )
    errors = [f"order_observation:{name}" for name, valid in checks if not valid]
    if observed_valid and observed > recv_wall_ns:
        errors.append("order_observation:observed_in_future")
    return tuple(errors)
