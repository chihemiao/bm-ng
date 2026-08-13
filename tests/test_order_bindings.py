import ast
import http.client
import inspect
import json
import socket
import threading
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from urllib.request import Request, urlopen

import pytest

from data import replay_order, schema_order_request
from execution import order_serde
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


@contextmanager
def _disconnect_fill_server():
    truth = {"status": "open", "submission_hits": 0, "status_hits": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            truth["client_order_id"] = request["client_order_id"]
            truth["submission_hits"] += 1
            self._send({"status": "open", "observed_ns": 101})

        def do_GET(self):
            truth["status"] = "filled"
            truth["status_hits"] += 1
            body = json.dumps({"status": "filled", "observed_ns": 103}).encode()
            if truth["status_hits"] == 1:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body) + 5))
                self.end_headers()
                self.wfile.write(body[: len(body) // 2])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self._send({"status": "filled", "observed_ns": 103})

        def _send(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", truth
    finally:
        server.shutdown()
        thread.join()


def _append_serialized_observation(root, boot_id, sequence, **changes):
    values = {
        "venue": "hyperliquid", "client_order_id": "client", "venue_order_id": "7",
        "status": "open", "observation_source": "submission_response",
        "observed_ns": 101, "venue_time_ms": None, "conn_id": "local-http",
        "boot_id": boot_id, "recv_wall_ns": 101 + sequence,
        "recv_mono_ns": 101 + sequence, "source": "test_adapter",
        "seq_within_boot": 1,
    }
    values.update(changes)
    writer = shard.ShardWriter(root, boot_id=boot_id)
    writer.append_event(order_serde.serialize_order_observation(**values))
    writer.close()


def _decision(intent, request, evidence):
    return decide_submission(
        intent, evidence, request,
        ReplayedDecisionHistory(intent.client_order_id, 0, False), 120, 50, 3,
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


def test_real_disconnect_fill_reconciles_without_a_duplicate_submission(tmp_path):
    intent = make_order_intent(
        "funding-carry", "git-deadbeef", 100, "hyperliquid",
        symbol="BTC", side="buy", quantity=Decimal("1"),
    )
    request = order_request_record(
        intent, 100, account_digest="a" * 64, lease_epoch=1,
        writer_instance_id="writer-one", wallet_fingerprint="b" * 64,
        allocated_nonce=7,
    )
    root = tmp_path / "disconnect-fill"
    with _disconnect_fill_server() as (base_url, truth):
        body = json.dumps({"client_order_id": intent.client_order_id}).encode()
        with urlopen(Request(base_url, data=body, method="POST"), timeout=1) as response:
            opened = json.load(response)
        _append_serialized_observation(
            root, "boot-open", 0, client_order_id=intent.client_order_id,
            status=opened["status"], observed_ns=opened["observed_ns"],
        )
        evidence = _assembled(
            shard.replay_event_window(root, 0, 200), client=intent.client_order_id,
        )
        assert evidence == ReconciliationEvidence("open", 101, None, None)
        assert _decision(intent, request, evidence) == "hold"

        with pytest.raises(http.client.IncompleteRead):
            with urlopen(f"{base_url}/orderStatus", timeout=1) as response:
                response.read()
        _append_serialized_observation(
            root, "boot-unknown", 1, client_order_id=intent.client_order_id,
            venue_order_id=None, status="unknown", observation_source="no_venue_response",
            observed_ns=102,
        )
        unknown = _assembled(
            shard.replay_event_window(root, 0, 200), client=intent.client_order_id,
        )
        assert unknown == ReconciliationEvidence("unknown", None, None, None)
        assert _decision(intent, request, unknown) == "reconcile"

        with urlopen(f"{base_url}/orderStatus", timeout=1) as response:
            filled = json.load(response)
        _append_serialized_observation(
            root, "boot-filled", 2, client_order_id=intent.client_order_id,
            status=filled["status"], observation_source="order_status",
            observed_ns=filled["observed_ns"], venue_time_ms=1,
        )
        final = _assembled(
            shard.replay_event_window(root, 0, 200), client=intent.client_order_id,
        )
        assert final == ReconciliationEvidence("filled", 103, None, None)
        assert _decision(intent, request, final) == "hold"
        assert truth == {"status": "filled", "submission_hits": 1, "status_hits": 2,
                         "client_order_id": intent.client_order_id}
