from importlib import import_module

import pytest

from data.contracts import validate_envelope
from data.schema_dispatch import ORDER_STATUSES
from tests.test_contracts import market_event

schema = import_module("data.schema_order_observation")
FIELDS = ("status", "source", "observed_ns", "venue_time_ms")
SOURCES = ("submission_response", "order_status", "execution_history", "no_venue_response")


def _payload(source="submission_response", status="open", observed_ns=999, venue_time_ms=None):
    return {"status": status, "source": source, "observed_ns": observed_ns,
            "venue_time_ms": venue_time_ms}


def _errors(payload=None, **changes):
    arguments = {
        "venue": "hyperliquid", "has_sequence": True, "identity_status": "known",
        "client_order_id": "0xclient", "venue_order_id": "7", "recv_wall_ns": 1_000}
    arguments.update(changes)
    return schema.order_observation_binding_errors(payload or _payload(), **arguments)


@pytest.mark.parametrize(
    ("source", "status", "oid", "venue_time_ms"),
    [
        ("no_venue_response", "unknown", None, None),
        ("submission_response", "open", "1", None),
        ("submission_response", "partially_filled", "1", 7),
        ("submission_response", "filled", "1", None),
        ("submission_response", "rejected", None, None),
        ("order_status", "absent", None, None),
        *(("order_status", status, "1", 7) for status in
          ("open", "partially_filled", "filled", "cancelled", "rejected")),
        *(("execution_history", status, "1", 7) for status in
          ("partially_filled", "filled")),
    ],
)
def test_bound_observation_source_status_matrix(source, status, oid, venue_time_ms):
    assert _errors(_payload(source, status, venue_time_ms=venue_time_ms),
                   venue_order_id=oid) == ()


def test_schema_reuses_status_registry_and_keeps_ids_only_in_envelope():
    payload = _payload()
    assert schema.ORDER_STATUSES is ORDER_STATUSES
    assert schema.FIELDS == FIELDS and set(payload) == set(FIELDS)
    assert not {"client_order_id", "venue_order_id"} & payload.keys()
    payload["client_order_id"] = "duplicate"
    assert _errors(payload) == ("order_observation:invalid_fields",)


@pytest.mark.parametrize("missing", (*FIELDS, "sequence"))
def test_bound_fields_and_sequence_are_one_atomic_presence_group(missing):
    payload = _payload()
    if missing != "sequence":
        payload.pop(missing)
    assert _errors(payload, has_sequence=missing != "sequence") == (
        f"order_observation:partial_binding:{missing}",
    )


def test_legacy_and_partial_classification_are_distinct():
    legacy = {"raw": "legacy"}
    assert schema.order_observation_binding_is_legacy(legacy, has_sequence=False)
    partial = {"status": "open"}
    assert not schema.order_observation_binding_is_legacy(partial, has_sequence=False)
    assert _errors(partial, has_sequence=False) == (
        "order_observation:partial_binding:observed_ns,sequence,source,venue_time_ms",
    )


@pytest.mark.parametrize(
    ("source", "status", "oid", "venue_time_ms"),
    [
        ("no_venue_response", "open", None, None),
        ("no_venue_response", "unknown", None, 1),
        ("no_venue_response", "unknown", "1", None),
        ("order_status", "absent", "1", None),
        ("order_status", "absent", None, 1),
        ("execution_history", "absent", "1", 1),
        ("submission_response", "open", None, None),
        ("order_status", "open", "1", None),
        ("execution_history", "filled", "1", None),
        *((source, "pending", "1", 7) for source in SOURCES),
    ],
)
def test_impossible_source_status_and_presence_combinations_fail_closed(
    source, status, oid, venue_time_ms,
):
    assert _errors(_payload(source, status, venue_time_ms=venue_time_ms),
                   venue_order_id=oid) == ("order_observation:invalid_combination",)


def test_bound_errors_accumulate_in_fixed_order_and_gate_cross_field_checks():
    payload = _payload("invented", "invented", observed_ns=True, venue_time_ms=True)
    errors = _errors(
        payload, venue="invented", identity_status="unknown",
        client_order_id="", venue_order_id=None,
    )
    assert errors == (
        "order_observation:invalid_status", "order_observation:invalid_source",
        "order_observation:invalid_observed_ns", "order_observation:invalid_venue_time_ms",
        "order_observation:unknown_venue:invented", "order_observation:identity_not_known",
        "order_observation:invalid_client_order_id",
    )
    assert _errors(_payload(observed_ns=1_001)) == (
        "order_observation:observed_in_future",
    )


def test_known_gap_bound_observations_are_not_yet_wired_into_the_envelope():
    event = market_event()
    event.update(
        event_kind="order", payload_schema="order_observation", seq_within_boot=9,
        identity_status="known", client_order_id="0xclient", venue_order_id="7",
        payload=_payload(status="invented"),
    )
    assert validate_envelope(event) is event
