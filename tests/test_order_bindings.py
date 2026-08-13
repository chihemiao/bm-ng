import ast
import inspect
from dataclasses import FrozenInstanceError
from importlib import import_module

import pytest

from data import replay_order, schema_order_request
from tests.test_shard import _ns, _order_request

shard = import_module("data.shard")


def _observation(
    sequence, *, venue="hyperliquid", client_order_id="0xrequest",
    venue_order_id="7", source="submission_response", status="open",
):
    event = _order_request(sequence, venue=venue, client_order_id=client_order_id)
    timed = {
        ("order_status", state): 1
        for state in ("open", "partially_filled", "filled", "cancelled", "rejected")
    } | {
        ("execution_history", state): 1
        for state in ("partially_filled", "filled")
    }
    event.update(
        payload_schema="order_observation", venue_order_id=venue_order_id,
        payload={
            "status": status, "source": source,
            "observed_ns": event["recv_wall_ns"] - 1,
            "venue_time_ms": timed.get((source, status)),
        },
    )
    return event


def _replay(root, *events):
    writer = shard.ShardWriter(root, boot_id="boot-a")
    for event in events:
        writer.append(shard.encode_event(event), event["recv_wall_ns"])
    writer.close()
    return shard.replay_event_window(root, _ns(4), _ns(5))


def _binding(venue="hyperliquid", client="0xrequest", oid="7"):
    return shard.OrderBinding(
        venue=venue, client_order_id=client, venue_order_id=oid,
    )


def test_order_binding_is_frozen_keyword_only_evidence():
    binding = _binding()
    with pytest.raises(TypeError):
        shard.OrderBinding("hyperliquid", "0xrequest", "7")
    with pytest.raises(FrozenInstanceError):
        binding.venue_order_id = "8"


def test_replay_binding_delegates_to_the_shared_request_classifier():
    assert (
        replay_order.order_request_event_binding
        is schema_order_request.order_request_event_binding
    )
    tree = ast.parse(inspect.getsource(replay_order))
    called = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "order_request_event_binding" in called


def test_replay_deduplicates_sources_and_exact_observation_retries(tmp_path):
    request = _order_request(1)
    submitted = _observation(2)
    status = _observation(3, source="order_status", status="filled")
    replay = _replay(tmp_path, request, submitted, submitted, status)

    assert replay.order_bindings == (_binding(),)
    assert len(replay.duplicate_digests) == 1
    assert replay.freeze_reasons == ()


def test_observations_without_venue_ids_do_not_create_bindings(tmp_path):
    request = _order_request(1)
    events = (
        _observation(
            2, venue_order_id=None, source="no_venue_response", status="unknown",
        ),
        _observation(3, venue_order_id=None, status="rejected"),
        _observation(
            4, venue_order_id=None, source="order_status", status="absent",
        ),
    )
    assert _replay(tmp_path, request, *events).order_bindings == ()


def test_missing_request_is_a_window_boundary_but_not_prior_request_freezes(tmp_path):
    missing = _replay(tmp_path / "missing", _observation(1))
    not_prior = _replay(
        tmp_path / "not-prior", _observation(1), _order_request(2),
    )

    assert missing.order_bindings == (_binding(),)
    assert missing.freeze_reasons == ()
    assert not_prior.order_bindings == (_binding(),)
    assert not_prior.freeze_reasons == (
        "order_observation:request_not_prior:hyperliquid:0xrequest",
    )


def test_one_client_bound_to_multiple_oids_freezes_and_keeps_evidence(tmp_path):
    replay = _replay(
        tmp_path, _order_request(1), _observation(2, venue_order_id="8"),
        _observation(3, venue_order_id="7", status="rejected"),
    )

    assert replay.order_bindings == (_binding(oid="7"), _binding(oid="8"))
    assert replay.freeze_reasons == (
        "order_observation:client_order_id_conflict:hyperliquid:0xrequest",
    )


def test_one_bybit_oid_bound_to_multiple_clients_freezes_without_int_coercion(tmp_path):
    events = (
        _order_request(1, venue="bybit", client_order_id="0xone"),
        _order_request(2, venue="bybit", client_order_id="0xtwo"),
        _observation(3, venue="bybit", client_order_id="0xone", venue_order_id="0007"),
        _observation(4, venue="bybit", client_order_id="0xtwo", venue_order_id="0007"),
    )
    replay = _replay(tmp_path, *events)

    assert replay.order_bindings == (
        _binding("bybit", "0xone", "0007"),
        _binding("bybit", "0xtwo", "0007"),
    )
    assert replay.freeze_reasons == (
        "order_observation:venue_order_id_conflict:bybit:0007",
    )
