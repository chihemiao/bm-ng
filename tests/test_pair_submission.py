import json
import socket
import threading
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from inspect import Parameter, signature
from typing import get_type_hints
from urllib.request import Request, urlopen

import pytest

from data import shard
from execution.nonce import NonceAllocator, SignerFence
from execution.order_serde import serialize_order_observation, serialize_order_request
from execution.orders import (
    OrderRequestRecord,
    ReconciliationEvidence,
    ReplayedDecisionHistory,
    make_t0a_pair_intents,
)
from execution.submission import PairLegSubmissionInputs, PairSubmissionOutcome, submit_t0a_pair
from execution.writer import WriterIdentity, WriterLease
from reconciliation.legs import (
    PairState,
    build_order_reconciliation_evidence,
    build_replayed_fill_pair_state,
)

WALLET = "b" * 64
PAIR = make_t0a_pair_intents(
    "funding-carry", "git-deadbeef", 100, symbol="BTC", quantity=Decimal("1")
)
PAIR_DATACLASS_CASES = [
    (
        PairLegSubmissionInputs,
        {
            "evidence": ReconciliationEvidence("absent", 101, 102, 103),
            "request": None,
            "history": ReplayedDecisionHistory(PAIR.hyperliquid.client_order_id, 0, False),
            "transport": pytest.fail,
        },
        [
            ("evidence", ReconciliationEvidence),
            ("request", OrderRequestRecord | None),
            ("history", ReplayedDecisionHistory),
            ("transport", Callable[[OrderRequestRecord], object]),
        ],
    ),
    (
        PairSubmissionOutcome,
        {"hyperliquid": None, "bybit": None},
        [("hyperliquid", object | BaseException), ("bybit", object | BaseException)],
    ),
]


def test_pair_submission_signature_is_pinned():
    contract = signature(submit_t0a_pair)
    names = [
        "pair", "hyperliquid", "bybit", "lease", "allocator", "request_recorder",
        "now_ns", "max_signal_age_ns", "max_reconcile_attempts", "now_ms", "decided_ns",
    ]
    assert [(item.name, item.kind) for item in contract.parameters.values()] == [
        (name, Parameter.KEYWORD_ONLY) for name in names
    ]
    assert contract.return_annotation is PairSubmissionOutcome


@pytest.mark.parametrize(("contract", "kwargs", "expected_fields"), PAIR_DATACLASS_CASES)
def test_pair_submission_dataclass_contract_is_pinned(contract, kwargs, expected_fields):
    hints = get_type_hints(contract)
    assert [(item.name, hints[item.name]) for item in fields(contract)] == expected_fields
    with pytest.raises(TypeError):
        contract(*kwargs.values())
    value = contract(**kwargs)
    with pytest.raises(FrozenInstanceError):
        setattr(value, next(iter(kwargs)), None)
    with pytest.raises(AttributeError):
        object.__setattr__(value, "not_a_field", None)


@pytest.fixture
def server():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            if self.path in {"/drop", "/bybit-submit"}:
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            body = self.path.removeprefix("/").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            hits.append(self.path)
            payloads = {
                "/hl/orderStatus": {"status": "filled", "venue_order_id": "7"},
                "/bybit/orderStatus": {"status": "open", "venue_order_id": "B-7"},
                "/hl/fills": [[{
                    "closedPnl": "0", "coin": "BTC", "crossed": False,
                    "dir": "Open Short", "fee": "0", "feeToken": "USDC",
                    "hash": "0xabc", "oid": 7, "px": "1", "side": "A",
                    "startPosition": "0", "sz": "1", "tid": 8, "time": 1000,
                }]],
                "/bybit/fills": [{
                    "retCode": 0, "retMsg": "OK", "retExtInfo": {}, "time": 1000,
                    "result": {"category": "linear", "nextPageCursor": "", "list": []},
                }],
            }
            body = json.dumps(payloads[self.path]).encode()
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
    assert [(row.leg, row.client_order_id) for row in recorded] == [
        ("hyperliquid", PAIR.hyperliquid.client_order_id),
        ("bybit", PAIR.bybit.client_order_id),
    ]
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


def _query_pair_truth(base_url, root):
    observations = shard.ShardWriter(root, boot_id="boot-query")
    statuses = {}
    for sequence, (venue, route, intent) in enumerate(
        (("hyperliquid", "hl", PAIR.hyperliquid), ("bybit", "bybit", PAIR.bybit)), 1
    ):
        with urlopen(f"{base_url}/{route}/orderStatus", timeout=1) as response:
            statuses[venue] = json.load(response)
        observations.append_event(serialize_order_observation(
            venue=venue, client_order_id=intent.client_order_id,
            venue_order_id=statuses[venue]["venue_order_id"],
            status=statuses[venue]["status"], observation_source="order_status",
            observed_ns=120, venue_time_ms=1, conn_id="local-http",
            boot_id="boot-query", recv_wall_ns=120 + sequence,
            recv_mono_ns=120 + sequence, source="order_status",
            seq_within_boot=sequence,
        ))
    observations.close()
    pages = []
    for route in ("hl", "bybit"):
        with urlopen(f"{base_url}/{route}/fills", timeout=1) as response:
            pages.append(json.load(response))
    return tuple(pages)


def test_bybit_disconnect_is_reconciled_as_accepted_unfilled(
    runtime, server, tmp_path,
):
    base_url, hits = server
    root = tmp_path / "pair-events"
    writer = shard.ShardWriter(root, boot_id="boot-submit")
    recorded, errors = [], []

    def record(request):
        intent = PAIR.hyperliquid if request.leg == "hyperliquid" else PAIR.bybit
        sequence = len(recorded) + 1
        writer.append_event(serialize_order_request(
            intent, request, conn_id="local-http", boot_id="boot-submit",
            recv_wall_ns=110 + sequence, recv_mono_ns=110 + sequence,
            source="execution", seq_within_boot=sequence,
        ))
        recorded.append(request)

    def transport(path):
        def call(request):
            assert recorded[-1] is request
            return _transport(base_url, path, errors)(request)
        return call

    outcome = _submit(
        runtime, _leg(PAIR.hyperliquid, transport("/hl-submit")),
        _leg(PAIR.bybit, transport("/bybit-submit")), record,
    )
    writer.close()
    assert outcome.hyperliquid == ("persist", b"hl-submit")
    assert len(errors) == 1 and outcome.bybit is errors[0]
    assert [row.leg for row in recorded] == ["hyperliquid", "bybit"]

    hl_pages, bybit_pages = _query_pair_truth(base_url, root)
    replay = shard.replay_event_window(root, 0, 200)
    assert build_order_reconciliation_evidence(
        replay, client_order_id=PAIR.hyperliquid.client_order_id, venue="hyperliquid",
    ) == ReconciliationEvidence("filled", 120, None, None)
    assert build_order_reconciliation_evidence(
        replay, client_order_id=PAIR.bybit.client_order_id, venue="bybit",
    ) == ReconciliationEvidence("open", 120, None, None)
    assert build_replayed_fill_pair_state(
        PAIR, replay=replay, hyperliquid_pages=hl_pages, bybit_pages=bybit_pages,
        since_ms=1000, skew_allowance_ms=0, observed_ns=130,
        page_complete=True, truncated=False, now_ns=140, max_age_ns=10,
    ) == PairState("imbalanced", (("bybit", "none"),))
    assert hits == [
        "/hl-submit", "/bybit-submit", "/hl/orderStatus", "/bybit/orderStatus",
        "/hl/fills", "/bybit/fills",
    ]
