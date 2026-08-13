import ast
import inspect
from dataclasses import FrozenInstanceError
from decimal import Decimal
from importlib import import_module

import pytest

from data import replay_order, schema_order_request
from execution.orders import (
    ReconciliationEvidence,
    ReplayedDecisionHistory,
    decide_submission,
    make_order_intent,
    order_request_record,
)
from reconciliation.legs import build_order_reconciliation_evidence
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


def _assembled(replay, *, client="0xrequest", venue="hyperliquid"):
    return build_order_reconciliation_evidence(
        replay, client_order_id=client, venue=venue,
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


def test_order_evidence_assembler_requires_a_replay_and_named_identity(tmp_path):
    class ReplayLike:
        events = ()
        freeze_reasons = ()

    parameters = inspect.signature(build_order_reconciliation_evidence).parameters
    assert tuple(parameters) == ("replay", "client_order_id", "venue")
    assert all(parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
               for name in ("client_order_id", "venue"))
    replay = _replay(tmp_path, _observation(1))
    with pytest.raises(TypeError, match="replay"):
        _assembled(object())
    with pytest.raises(TypeError, match="replay"):
        _assembled(ReplayLike())
    with pytest.raises(TypeError, match="client_order_id"):
        _assembled(replay, client=1)
    with pytest.raises(ValueError, match="client_order_id"):
        _assembled(replay, client="")
    with pytest.raises(TypeError, match="venue"):
        _assembled(replay, venue=1)
    with pytest.raises(ValueError, match="venue"):
        _assembled(replay, venue="invented")


def test_missing_and_no_response_observations_are_fully_unknown(tmp_path):
    no_response = _observation(
        1, venue_order_id=None, source="no_venue_response", status="unknown",
    )
    replay = _replay(tmp_path, no_response)
    unknown = ReconciliationEvidence("unknown", None, None, None)
    assert _assembled(replay) == unknown
    assert _assembled(replay, client="other") == unknown
    assert _assembled(replay, venue="bybit") == unknown


def test_latest_observation_time_selects_only_the_orders_face(tmp_path):
    newer_observed = _observation(1, source="order_status", status="filled")
    newer_observed["payload"]["observed_ns"] = 110
    newer_recv = _observation(2, status="open")
    newer_recv["payload"]["observed_ns"] = 100
    replay = _replay(tmp_path, newer_observed, newer_recv)
    assert _assembled(replay) == ReconciliationEvidence("filled", 110, None, None)


def test_latest_time_ties_must_agree_or_become_fully_unknown(tmp_path):
    submitted = _observation(1, status="open")
    queried = _observation(2, source="order_status", status="open")
    submitted["payload"]["observed_ns"] = queried["payload"]["observed_ns"] = 110
    assert _assembled(_replay(tmp_path / "agree", submitted, queried)) == (
        ReconciliationEvidence("open", 110, None, None)
    )
    queried["payload"]["status"] = "filled"
    assert _assembled(_replay(tmp_path / "conflict", submitted, queried)) == (
        ReconciliationEvidence("unknown", None, None, None)
    )


def test_replay_freeze_discards_even_an_unambiguous_observation_time(tmp_path):
    replay = _replay(
        tmp_path, _order_request(1), _observation(2, venue_order_id="8"),
        _observation(3, venue_order_id="7", source="order_status", status="filled"),
    )
    assert replay.freeze_reasons
    assert _assembled(replay) == ReconciliationEvidence("unknown", None, None, None)


def test_explicit_absence_cannot_invent_fill_or_position_query_times(tmp_path):
    observed = _observation(
        1, venue_order_id=None, source="order_status", status="absent",
    )
    observed["payload"]["observed_ns"] = 111
    evidence = _assembled(_replay(tmp_path, observed))
    assert evidence == ReconciliationEvidence("absent", 111, None, None)

    intent = make_order_intent(
        "funding-carry", "git-deadbeef", 100, "hyperliquid",
        symbol="BTC", side="buy", quantity=Decimal("1"),
    )
    request = order_request_record(
        intent, 110, account_digest="a" * 64, lease_epoch=1,
        writer_instance_id="writer-one", wallet_fingerprint="b" * 64,
        allocated_nonce=7,
    )
    history = ReplayedDecisionHistory(intent.client_order_id, 0, False)
    assert decide_submission(intent, evidence, request, history, 120, 50, 3) == "reconcile"
