from importlib import import_module

import pytest

from data.contracts import ContractError, validate_envelope
from data.schema_dispatch import DURABLE_EVENT_SCHEMAS, ORDER_STATUSES
from data.shard import ShardWriter
from tests.test_contracts import market_event

schema = import_module("data.schema_order_observation")
FIELDS = ("status", "source", "observed_ns", "venue_time_ms")
SOURCES = ("submission_response", "order_status", "execution_history", "no_venue_response")


def _event(source="submission_response", status="open", oid="7", venue_time_ms=None):
    event = market_event()
    event.update(
        event_kind="order",
        payload_schema="order_observation",
        seq_within_boot=9,
        identity_status="known",
        client_order_id="0xclient",
        venue_order_id=oid,
        payload={
            "status": status,
            "source": source,
            "observed_ns": 999,
            "venue_time_ms": venue_time_ms,
        },
    )
    return event


@pytest.mark.parametrize(
    ("source", "status", "oid", "venue_time_ms"),
    [
        ("no_venue_response", "unknown", None, None),
        ("submission_response", "open", "1", None),
        ("submission_response", "partially_filled", "1", 7),
        ("submission_response", "filled", "1", None),
        ("submission_response", "rejected", None, None),
        ("order_status", "absent", None, None),
        *(
            ("order_status", status, "1", 7)
            for status in ("open", "partially_filled", "filled", "cancelled", "rejected")
        ),
        *(("execution_history", status, "1", 7) for status in ("partially_filled", "filled")),
    ],
)
def test_bound_observation_source_status_matrix(source, status, oid, venue_time_ms):
    assert validate_envelope(_event(source, status, oid, venue_time_ms))


def test_schema_reuses_status_registry_and_keeps_ids_only_in_envelope():
    event = _event()
    assert schema.ORDER_STATUSES is ORDER_STATUSES
    assert "order_observation" in DURABLE_EVENT_SCHEMAS
    assert set(event["payload"]) == set(FIELDS)
    assert not {"client_order_id", "venue_order_id"} & event["payload"].keys()
    event["payload"]["client_order_id"] = "duplicate"
    with pytest.raises(ContractError, match="invalid_fields"):
        validate_envelope(event)


@pytest.mark.parametrize("missing", (*FIELDS, "seq_within_boot"))
def test_bound_fields_and_sequence_are_one_atomic_presence_group(missing):
    event = _event()
    target = event if missing == "seq_within_boot" else event["payload"]
    target.pop(missing)
    expected = "sequence" if missing == "seq_within_boot" else missing
    with pytest.raises(ContractError) as error:
        validate_envelope(event)
    assert str(error.value) == f"order_observation:partial_binding:{expected}"


def test_unbound_legacy_observation_replays_but_cannot_be_appended(tmp_path):
    event = _event()
    event.pop("seq_within_boot")
    event.update(identity_status="unknown", client_order_id=None, venue_order_id="unowned")
    event["payload"] = {"raw": "legacy"}
    assert schema.order_observation_binding_is_legacy(event["payload"], has_sequence=False)
    assert validate_envelope(event) is event
    with pytest.raises(ContractError, match="legacy order observation"):
        ShardWriter(tmp_path, "boot-1").append_event(event)


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
    source,
    status,
    oid,
    venue_time_ms,
):
    with pytest.raises(ContractError, match="order_observation:invalid_combination"):
        validate_envelope(_event(source, status, oid, venue_time_ms))


def test_bound_value_and_envelope_errors_accumulate_in_fixed_order():
    payload = {
        "status": "invented",
        "source": "invented",
        "observed_ns": True,
        "venue_time_ms": True,
    }
    errors = schema.order_observation_binding_errors(
        payload,
        venue="invented",
        has_sequence=True,
        identity_status="unknown",
        client_order_id="",
        venue_order_id=None,
        recv_wall_ns=1_000,
    )
    assert errors == (
        "order_observation:invalid_status",
        "order_observation:invalid_source",
        "order_observation:invalid_observed_ns",
        "order_observation:invalid_venue_time_ms",
        "order_observation:unknown_venue:invented",
        "order_observation:identity_not_known",
        "order_observation:invalid_client_order_id",
    )
    event = _event()
    event["identity_status"] = "unknown"
    with pytest.raises(ContractError, match="identity_not_known"):
        validate_envelope(event)
    event = _event()
    event["payload"]["observed_ns"] = 1_001
    with pytest.raises(ContractError, match="observed_in_future"):
        validate_envelope(event)
