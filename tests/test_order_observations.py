import ast
import inspect
from importlib import import_module

import pytest

from data import schema_order_request
from data.contracts import ContractError, validate_envelope
from data.schema_dispatch import DURABLE_EVENT_SCHEMAS, ORDER_STATUSES
from tests.test_contracts import market_event

schema = import_module("data.schema_order_observation")
shard = import_module("data.shard")
FIELDS = ("status", "source", "observed_ns", "venue_time_ms")
SOURCES = ("submission_response", "order_status", "execution_history", "no_venue_response")


def _payload(source="submission_response", status="open", observed_ns=999, venue_time_ms=None):
    return {"status": status, "source": source, "observed_ns": observed_ns,
            "venue_time_ms": venue_time_ms}


def _event():
    event = market_event()
    event.update(
        event_kind="order", payload_schema="order_observation", seq_within_boot=9,
        identity_status="known", client_order_id="0xclient", venue_order_id="7",
        payload=_payload(),
    )
    return event


def _errors(payload=None, **changes):
    arguments = {
        "venue": "hyperliquid", "has_sequence": True, "identity_status": "known",
        "client_order_id": "0xclient", "venue_order_id": "7", "recv_wall_ns": 1_000}
    arguments.update(changes)
    return schema.order_observation_binding_errors(payload or _payload(), **arguments)


def test_schema_reuses_status_registry_and_keeps_ids_only_in_envelope():
    payload = _payload()
    assert schema.ORDER_STATUSES is ORDER_STATUSES
    assert schema.FIELDS == FIELDS and set(payload) == set(FIELDS)
    assert not {"client_order_id", "venue_order_id"} & payload.keys()
    payload["client_order_id"] = "duplicate"
    assert _errors(payload) == ("order_observation:invalid_fields",)


def test_schema_calls_the_shared_public_presence_classifier():
    assert schema.binding_presence is schema_order_request.binding_presence
    tree = ast.parse(inspect.getsource(schema))
    called = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "binding_presence" in called


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
        ("no_venue_response", "unknown", None, None),
        ("submission_response", "open", "1", None),
        ("submission_response", "partially_filled", "1", 7),
        ("submission_response", "filled", "1", None),
        ("submission_response", "rejected", None, None),
        ("submission_response", "rejected", "1", 7),
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


@pytest.mark.parametrize(
    ("source", "status", "oid", "venue_time_ms"),
    [
        ("no_venue_response", "open", None, None),
        ("no_venue_response", "unknown", None, 1),
        *(("no_venue_response", "unknown", oid, None) for oid in ("1", "", 0, True)),
        ("order_status", "absent", "1", None),
        ("order_status", "absent", None, 1),
        ("execution_history", "absent", "1", 1),
        *(("submission_response", "open", oid, None) for oid in (None, "", 0, True)),
        *(("submission_response", "rejected", oid, None) for oid in ("", 0, True)),
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
        client_order_id="",
    )
    assert errors == (
        "order_observation:invalid_status", "order_observation:invalid_source",
        "order_observation:invalid_observed_ns", "order_observation:invalid_venue_time_ms",
        "order_observation:unknown_venue:invented", "order_observation:identity_not_known",
        "order_observation:invalid_client_order_id",
    )
    assert _errors(_payload("invented", "open")) == (
        "order_observation:invalid_source",
    )
    assert _errors(_payload(observed_ns=1_001)) == (
        "order_observation:observed_in_future",
    )


def test_bound_observation_errors_are_enforced_by_the_envelope():
    event = market_event()
    event.update(
        event_kind="order", payload_schema="order_observation", seq_within_boot=9,
        identity_status="known", client_order_id="0xclient", venue_order_id="7",
        payload=_payload(status="invented"),
    )
    with pytest.raises(ContractError, match="^order_observation:invalid_status$"):
        validate_envelope(event)


def test_bound_observation_requires_a_sequence_in_the_envelope():
    event = market_event()
    event.update(
        event_kind="order", payload_schema="order_observation",
        identity_status="known", client_order_id="0xclient", venue_order_id="7",
        payload=_payload(),
    )
    with pytest.raises(ContractError, match="^order_observation:partial_binding:sequence$"):
        validate_envelope(event)


def test_order_observation_is_registered_as_durable_evidence():
    assert "order_observation" in DURABLE_EVENT_SCHEMAS


def test_bound_order_observation_uses_the_durable_writer(tmp_path):
    event = _event()
    writer = shard.ShardWriter(tmp_path, boot_id="boot-1")
    writer.append_event(event)
    writer.close()
    assert shard.replay_event_window(tmp_path, 0, 2_000).events == (event,)


def test_legacy_order_observation_replays_but_cannot_be_appended(tmp_path):
    legacy = _event()
    legacy.pop("seq_within_boot")
    legacy["payload"] = {"raw": "legacy"}
    writer = shard.ShardWriter(tmp_path, boot_id="boot-1")
    writer.append(shard.encode_event(legacy), legacy["recv_wall_ns"])
    writer.close()
    assert shard.replay_event_window(tmp_path, 0, 2_000).events == (legacy,)

    rejected = shard.ShardWriter(tmp_path / "legacy", boot_id="boot-1")
    with pytest.raises(ContractError, match="legacy order observation"):
        rejected.append_event(legacy)
    rejected.close()
