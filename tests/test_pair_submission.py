import socket
import threading
from dataclasses import replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest

from execution.nonce import NonceAllocator, SignerFence
from execution.orders import ReconciliationEvidence, ReplayedDecisionHistory, make_t0a_pair_intents
from execution.submission import PairLegSubmissionInputs, PairSubmissionOutcome, submit_t0a_pair
from execution.writer import WriterIdentity, WriterLease

WALLET = "b" * 64
PAIR = make_t0a_pair_intents(
    "funding-carry", "git-deadbeef", 100, symbol="BTC", quantity=Decimal("1")
)


@pytest.fixture
def server():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            if self.path == "/drop":
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            body = self.path.removeprefix("/").encode()
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
    identity = WriterIdentity("test-account", "writer-one", WALLET, "boot-one")
    lease = WriterLease.acquire(tmp_path, identity, [].append, acquired_ns=90)
    lease._authority = lease.authority._replace(mode="risk_increasing")
    fence = SignerFence.acquire(tmp_path, WALLET, "writer-one")
    allocator = NonceAllocator(
        fence, lease, account_digest="a" * 64, replayed_last=0,
        replayed_freeze_reason=None, recorder=lambda _row: None,
    )
    yield lease, allocator
    fence.release()
    lease.release()


def _leg(intent, transport, *, status="absent"):
    return PairLegSubmissionInputs(
        evidence=ReconciliationEvidence(status, 101, 102, 103),
        request=None,
        history=ReplayedDecisionHistory(intent.client_order_id, 0, False),
        transport=transport,
    )


def _transport(base_url, path, errors):
    def call(_request):
        try:
            with urlopen(Request(base_url + path, data=b"{}"), timeout=1) as response:
                return response.read()
        except BaseException as error:
            errors.append(error)
            raise
    return call


def _submit(runtime, hl, bybit, recorder, *, pair=PAIR):
    lease, allocator = runtime
    return submit_t0a_pair(
        pair=pair, hyperliquid=hl, bybit=bybit, lease=lease, allocator=allocator,
        request_recorder=recorder, now_ns=120, max_signal_age_ns=50,
        max_reconcile_attempts=3, now_ms=500, decided_ns=110,
    )


def test_successes_are_hl_first_and_share_one_durable_recorder(runtime, server):
    base_url, hits = server
    recorded, errors = [], []
    result = _submit(
        runtime, _leg(PAIR.hyperliquid, _transport(base_url, "/hl", errors)),
        _leg(PAIR.bybit, _transport(base_url, "/bybit", errors)), recorded.append,
    )
    assert result == PairSubmissionOutcome(
        hyperliquid=("persist", b"hl"), bybit=("persist", b"bybit")
    )
    assert hits == ["/hl", "/bybit"] and errors == []
    assert [row.leg for row in recorded] == ["hyperliquid", "bybit"]
    assert [row.recorded_ns for row in recorded] == [110, 110]


@pytest.mark.parametrize(
    ("hl_path", "bybit_path"),
    [("/drop", "/bybit"), ("/hl", "/drop"), ("/drop", "/drop")],
)
def test_each_real_transport_failure_is_returned_without_skipping_the_other_leg(
    runtime, server, hl_path, bybit_path,
):
    base_url, hits = server
    errors = {"hyperliquid": [], "bybit": []}
    result = _submit(
        runtime, _leg(PAIR.hyperliquid, _transport(base_url, hl_path, errors["hyperliquid"])),
        _leg(PAIR.bybit, _transport(base_url, bybit_path, errors["bybit"])), [].append,
    )
    assert hits == [hl_path, bybit_path]
    for leg, path, success in (
        ("hyperliquid", hl_path, ("persist", b"hl")),
        ("bybit", bybit_path, ("persist", b"bybit")),
    ):
        expected = errors[leg][0] if path == "/drop" else success
        actual = getattr(result, leg)
        assert actual is expected if path == "/drop" else actual == expected


@pytest.mark.parametrize(
    "pair", [object(), replace(PAIR, hyperliquid=replace(PAIR.hyperliquid, side="buy"))]
)
def test_invalid_pair_fails_before_either_leg_is_called(runtime, server, pair):
    base_url, hits = server
    leg = _leg(PAIR.hyperliquid, _transport(base_url, "/hl", []))
    error = TypeError if type(pair) is object else ValueError
    with pytest.raises(error):
        _submit(runtime, leg, leg, [].append, pair=pair)
    assert hits == []


def test_non_transport_decisions_remain_independent_leg_outcomes(runtime):
    result = _submit(
        runtime, _leg(PAIR.hyperliquid, pytest.fail, status="open"),
        _leg(PAIR.bybit, pytest.fail, status="partially_filled"), [].append,
    )
    assert result == PairSubmissionOutcome(
        hyperliquid=("hold", None), bybit=("hold", None)
    )
