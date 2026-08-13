"""Observation semantics on inputs whose common envelope fields were prevalidated."""

from collections.abc import Mapping

from data.schema_dispatch import ORDER_LEGS, ORDER_STATUSES
from data.schema_order_request import binding_presence

FIELDS = ("status", "source", "observed_ns", "venue_time_ms")
# Venue stays in the envelope: unlike a request leg, it is not identity/preimage data.
_NONE, _REQUIRED, _OPTIONAL = range(3)
_RULES = {
    "no_venue_response": {"unknown": (_NONE, _NONE)},
    "submission_response": {
        status: (_OPTIONAL if status == "rejected" else _REQUIRED, _OPTIONAL)
        for status in ("open", "partially_filled", "filled", "rejected")
    },
    "order_status": {
        status: ((_NONE, _NONE) if status == "absent" else (_REQUIRED, _REQUIRED))
        for status in ("absent", "open", "partially_filled", "filled", "cancelled", "rejected")
    },
    "execution_history": {
        status: (_REQUIRED, _REQUIRED) for status in ("partially_filled", "filled")
    },
}
SOURCES = frozenset(_RULES)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _ns(value: object) -> bool:
    return type(value) is int and value >= 0


def _matches(value: object, present: bool, rule: int) -> bool:
    if rule == _NONE:
        return value is None
    if rule == _REQUIRED:
        return present
    return value is None or present


def _combination(source: object, status: object, oid: object, venue_time: object) -> bool:
    requirements = _RULES.get(source, {}).get(status)
    return requirements is not None and _matches(oid, _text(oid), requirements[0]) and (
        _matches(venue_time, venue_time is not None, requirements[1]))


def order_observation_binding_is_legacy(
    payload: Mapping[str, object], *, has_sequence: object) -> bool:
    return binding_presence(payload, has_sequence, FIELDS)[0]


def order_observation_binding_errors(
    payload: Mapping[str, object], *, venue: object, has_sequence: object,
    identity_status: object, client_order_id: object, venue_order_id: object,
    recv_wall_ns: object,
) -> tuple[str, ...]:
    legacy, missing = binding_presence(payload, has_sequence, FIELDS)
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
    if checks[1][1] and checks[2][1] and not _combination(
        source, status, venue_order_id, venue_time
    ):
        errors.append("order_observation:invalid_combination")
    if observed_valid and observed > recv_wall_ns:
        errors.append("order_observation:observed_in_future")
    return tuple(errors)
