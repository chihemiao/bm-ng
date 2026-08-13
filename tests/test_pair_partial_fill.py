import json
import socket
import threading
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest

from data import shard
from execution.nonce import NonceAllocator, SignerFence
from execution.order_serde import (
    rehydrate_order_request,
    serialize_order_observation,
    serialize_order_request,
)
from execution.orders import (
    ReconciliationEvidence,
    ReplayedDecisionHistory,
    make_t0a_pair_intents,
)
from execution.submission import (
    PairLegSubmissionInputs,
    PairSubmissionOutcome,
    submit_t0a_pair,
)
from execution.writer import WriterIdentity, WriterLease
from reconciliation.legs import (
    PairState,
    build_order_reconciliation_evidence,
    build_replayed_fill_pair_state,
)

PAIR = make_t0a_pair_intents(
    "funding-carry", "git-deadbeef", 100, symbol="BTC", quantity=Decimal("1")
)
HL_FILL = {
    "closedPnl": "0", "coin": "BTC", "crossed": False, "dir": "Open Short",
    "fee": "0", "feeToken": "USDC", "hash": "0xabc", "oid": 7, "px": "1",
    "side": "A", "startPosition": "0", "sz": "1", "tid": 8, "time": 1000,
}
BYBIT_FILL = {
    "symbol": "BTCUSDT", "orderLinkId": PAIR.bybit.client_order_id, "side": "Buy",
    "execId": "execution-1", "execQty": "0.4", "execType": "Trade", "execTime": "1000",
}
BYBIT_OTHER_FILL = {
    **BYBIT_FILL, "orderLinkId": "other-order", "execId": "execution-2", "execQty": "0.6",
}


@contextmanager
def _venue_server(status, venue_order_id, fill_page, *, drop_submit=False):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(("POST", self.path))
            self.rfile.read(int(self.headers["Content-Length"]))
            if drop_submit:
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self._send(b"accepted")

        def do_GET(self):
            hits.append(("GET", self.path))
            payload = (
                {"status": status, "venue_order_id": venue_order_id}
                if self.path == "/orderStatus" else fill_page
            )
            self._send(json.dumps(payload).encode())

        def _send(self, body):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    service = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{service.server_port}", hits
    finally:
        service.shutdown()
        thread.join()


@pytest.fixture
def runtime(tmp_path):
    identity = WriterIdentity("test-account", "writer-one", "b" * 64, "boot-one")
    lease = WriterLease.acquire(tmp_path, identity, [].append, acquired_ns=90)
    lease._authority = lease.authority._replace(mode="risk_increasing")
    fence = SignerFence.acquire(tmp_path, "b" * 64, "writer-one")
    allocator = NonceAllocator(
        fence, lease, account_digest="a" * 64, replayed_last=0,
        replayed_freeze_reason=None, recorder=lambda _row: None,
    )
    yield lease, allocator
    fence.release()
    lease.release()


def _leg(intent, url, recorded, *, request=None, evidence=None):
    def transport(request):
        assert request in recorded
        with urlopen(Request(url + "/submit", data=b"{}"), timeout=1) as response:
            return response.read()

    return PairLegSubmissionInputs(
        evidence=evidence or ReconciliationEvidence("absent", 101, 102, 103),
        request=request,
        history=ReplayedDecisionHistory(intent.client_order_id, 0, False),
        transport=transport,
    )


def _submit(runtime, hyperliquid, bybit, recorder):
    return submit_t0a_pair(
        pair=PAIR, hyperliquid=hyperliquid, bybit=bybit,
        lease=runtime[0], allocator=runtime[1], request_recorder=recorder,
        now_ns=120, max_signal_age_ns=50, max_reconcile_attempts=3,
        now_ms=500, decided_ns=110,
    )


def _recorder(writer, recorded):
    def record(request):
        intent = PAIR.hyperliquid if request.leg == "hyperliquid" else PAIR.bybit
        sequence = len(recorded) + 1
        writer.append_event(serialize_order_request(
            intent, request, conn_id="local-http", boot_id="boot-submit",
            recv_wall_ns=110 + sequence, recv_mono_ns=110 + sequence,
            source="execution", seq_within_boot=sequence,
        ))
        recorded.append(request)
    return record


def _query_truth(root, venues, *, fills=True):
    writer = shard.ShardWriter(root, boot_id="boot-query")
    pages = []
    for sequence, (venue, url, intent) in enumerate(venues, 1):
        with urlopen(url + "/orderStatus", timeout=1) as response:
            status = json.load(response)
        writer.append_event(serialize_order_observation(
            venue=venue, client_order_id=intent.client_order_id,
            venue_order_id=status["venue_order_id"], status=status["status"],
            observation_source="order_status", observed_ns=120, venue_time_ms=1,
            conn_id="local-http", boot_id="boot-query", recv_wall_ns=120 + sequence,
            recv_mono_ns=120 + sequence, source="order_status", seq_within_boot=sequence,
        ))
        if fills:
            with urlopen(url + "/fills", timeout=1) as response:
                pages.append(json.load(response))
    writer.close()
    return tuple(pages)


def _replayed_requests(replay):
    restored = {
        intent.leg: (intent, request)
        for event in replay.events
        if event["payload_schema"] == "order_request"
        for intent, request in (rehydrate_order_request(event),)
    }
    assert restored["hyperliquid"][0] == PAIR.hyperliquid
    assert restored["bybit"][0] == PAIR.bybit
    return {venue: value[1] for venue, value in restored.items()}


def _resume_leg(intent, url, recorded, requests, evidence):
    return _leg(
        intent, url, recorded, request=requests[intent.leg], evidence=evidence,
    )


def test_successful_pair_with_one_partial_fill_is_imbalanced(runtime, tmp_path):
    bybit_page = {
        "retCode": 0, "retMsg": "OK", "retExtInfo": {}, "time": 1000,
        "result": {
            "category": "linear", "nextPageCursor": "",
            "list": [BYBIT_FILL, BYBIT_OTHER_FILL],
        },
    }
    with _venue_server("filled", "7", [HL_FILL]) as (hl_url, hl_hits), \
            _venue_server("partially_filled", "B-7", bybit_page) as (by_url, by_hits):
        root = tmp_path / "pair-events"
        writer = shard.ShardWriter(root, boot_id="boot-submit")
        recorded = []
        outcome = _submit(
            runtime, _leg(PAIR.hyperliquid, hl_url, recorded),
            _leg(PAIR.bybit, by_url, recorded), _recorder(writer, recorded),
        )
        writer.close()
        assert outcome == PairSubmissionOutcome(
            hyperliquid=("persist", b"accepted"), bybit=("persist", b"accepted")
        )
        assert [row.leg for row in recorded] == ["hyperliquid", "bybit"]

        venues = (("hyperliquid", hl_url, PAIR.hyperliquid), ("bybit", by_url, PAIR.bybit))
        hl_pages, bybit_pages = _query_truth(root, venues)
        replay = shard.replay_event_window(root, 0, 200)
        assert build_order_reconciliation_evidence(
            replay, client_order_id=PAIR.hyperliquid.client_order_id, venue="hyperliquid",
        ) == ReconciliationEvidence("filled", 120, None, None)
        assert build_order_reconciliation_evidence(
            replay, client_order_id=PAIR.bybit.client_order_id, venue="bybit",
        ) == ReconciliationEvidence("partially_filled", 120, None, None)
        assert build_replayed_fill_pair_state(
            PAIR, replay=replay, hyperliquid_pages=(hl_pages,), bybit_pages=(bybit_pages,),
            since_ms=1000, skew_allowance_ms=0, observed_ns=130,
            page_complete=True, truncated=False, now_ns=140, max_age_ns=10,
        ) == PairState("imbalanced", (("bybit", "partial"),))
        expected = [("POST", "/submit"), ("GET", "/orderStatus"), ("GET", "/fills")]
        assert hl_url != by_url and hl_hits == expected and by_hits == expected


def test_pair_ack_loss_resume_never_retransports(runtime, tmp_path):
    with _venue_server("filled", "7", []) as (hl_url, hl_hits), _venue_server(
        "open", "B-7", {}, drop_submit=True,
    ) as (by_url, by_hits):
        root = tmp_path / "pair-events"
        writer = shard.ShardWriter(root, boot_id="boot-submit")
        recorded = []
        first = _submit(
            runtime, _leg(PAIR.hyperliquid, hl_url, recorded),
            _leg(PAIR.bybit, by_url, recorded), _recorder(writer, recorded),
        )
        writer.close()
        assert first.hyperliquid == ("persist", b"accepted")
        assert isinstance(first.bybit, OSError)
        replay = shard.replay_event_window(root, 0, 200)
        requests = _replayed_requests(replay)

        old = ReconciliationEvidence("absent", 101, 102, 103)
        pending = _submit(
            runtime, _resume_leg(PAIR.hyperliquid, hl_url, recorded, requests, old),
            _resume_leg(PAIR.bybit, by_url, recorded, requests, old),
            recorded.append,
        )
        assert pending == PairSubmissionOutcome(
            hyperliquid=("reconcile", None), bybit=("reconcile", None)
        )
        assert hl_hits == by_hits == [("POST", "/submit")]

        venues = (("hyperliquid", hl_url, PAIR.hyperliquid), ("bybit", by_url, PAIR.bybit))
        assert _query_truth(root, venues, fills=False) == ()
        replay = shard.replay_event_window(root, 0, 200)
        evidence = {
            venue: build_order_reconciliation_evidence(
                replay, client_order_id=intent.client_order_id, venue=venue,
            )
            for venue, _, intent in venues
        }
        assert evidence == {
            "hyperliquid": ReconciliationEvidence("filled", 120, None, None),
            "bybit": ReconciliationEvidence("open", 120, None, None),
        }
        held = _submit(
            runtime, _resume_leg(
                PAIR.hyperliquid, hl_url, recorded, requests, evidence["hyperliquid"],
            ),
            _resume_leg(PAIR.bybit, by_url, recorded, requests, evidence["bybit"]),
            recorded.append,
        )
        assert held == PairSubmissionOutcome(
            hyperliquid=("hold", None), bybit=("hold", None)
        )
        expected = [("POST", "/submit"), ("GET", "/orderStatus")]
        assert len(recorded) == 2 and hl_url != by_url
        assert hl_hits == expected and by_hits == expected
