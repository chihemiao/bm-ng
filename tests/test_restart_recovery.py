import json
import os
import signal
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from data.shard import ShardWriter, replay_event_window
from execution.nonce import NonceAllocator, SignerFence, replay_last_allocated_nonce
from execution.order_serde import (
    rehydrate_order_request,
    serialize_order_observation,
    serialize_order_request,
)
from execution.orders import ReconciliationEvidence, ReplayedDecisionHistory, make_order_intent
from execution.submission import submit_order
from execution.writer import WriterIdentity, WriterLease
from reconciliation.legs import build_order_reconciliation_evidence

ACCOUNT = "a" * 64
WALLET = "b" * 64


@contextmanager
def _venue_server():
    truth = {"client_order_id": None, "submission_hits": 0, "status_hits": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            truth["client_order_id"] = json.loads(body)["client_order_id"]
            truth["submission_hits"] += 1
            self.connection.shutdown(socket.SHUT_RDWR)

        def do_GET(self):
            truth["status_hits"] += 1
            body = json.dumps({
                "client_order_id": truth["client_order_id"], "status": "filled",
            }).encode()
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


def _runtime(root: Path, instance: str, boot: str, replayed_last: int):
    lease = WriterLease.acquire(
        root / "locks", WriterIdentity("hyperliquid:test", instance, WALLET, boot),
        lambda _row: None, acquired_ns=90,
    )
    lease._authority = lease.authority._replace(mode="risk_increasing")
    fence = SignerFence.acquire(root / "locks", WALLET, instance)
    allocator = NonceAllocator(
        fence, lease, account_digest=ACCOUNT, replayed_last=replayed_last,
        replayed_freeze_reason=None, recorder=lambda _row: None,
    )
    return lease, fence, allocator


def _post(base_url: str, request):
    body = json.dumps({"client_order_id": request.client_order_id}).encode()
    with urlopen(Request(base_url, data=body, method="POST"), timeout=2) as response:
        return response.read()


def _process_a(root: Path, base_url: str) -> None:
    writer = ShardWriter(root / "events", boot_id="boot-a")
    lease, _, allocator = _runtime(root, "writer-a", "boot-a", 0)
    intent = make_order_intent(
        "funding-carry", "git-deadbeef", 100, "hyperliquid",
        symbol="BTC", side="buy", quantity=Decimal("1"),
    )

    def record_request(request):
        writer.append_event(serialize_order_request(
            intent, request, conn_id="local-http", boot_id="boot-a",
            recv_wall_ns=102, recv_mono_ns=102, source="execution", seq_within_boot=1,
        ))
        writer.close()

    try:
        submit_order(
            intent, ReconciliationEvidence("absent", 101, 102, 103), None,
            ReplayedDecisionHistory(intent.client_order_id, 0, False), lease, allocator,
            lambda request: _post(base_url, request), record_request,
            now_ns=120, max_signal_age_ns=50, max_reconcile_attempts=3,
            now_ms=500, decided_ns=110,
        )
    except Exception:
        print("request-durable-before-crash", flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
    raise AssertionError("transport unexpectedly returned")


def _process_b(root: Path, base_url: str) -> None:
    replay = replay_event_window(root / "events", 0, 200)
    event = next(row for row in replay.events if row["payload_schema"] == "order_request")
    intent, request = rehydrate_order_request(event)
    with urlopen(f"{base_url}/orderStatus", timeout=2) as response:
        status = json.load(response)
    writer = ShardWriter(root / "events", boot_id="boot-b")
    writer.append_event(serialize_order_observation(
        venue="hyperliquid", client_order_id=status["client_order_id"], venue_order_id="7",
        status=status["status"], observation_source="order_status", observed_ns=130,
        venue_time_ms=1, conn_id="local-http", boot_id="boot-b", recv_wall_ns=131,
        recv_mono_ns=131, source="order_status", seq_within_boot=1,
    ))
    writer.close()
    replay = replay_event_window(root / "events", 0, 200)
    evidence = build_order_reconciliation_evidence(
        replay, client_order_id=intent.client_order_id, venue="hyperliquid",
    )
    lease, fence, allocator = _runtime(
        root, "writer-b", "boot-b", replay_last_allocated_nonce(replay.events, WALLET),
    )
    result = submit_order(
        intent, evidence, request, ReplayedDecisionHistory(intent.client_order_id, 0, False),
        lease, allocator, lambda value: _post(base_url, value), lambda _row: None,
        now_ns=140, max_signal_age_ns=50, max_reconcile_attempts=3,
        now_ms=500, decided_ns=140,
    )
    print(json.dumps({"decision": result[0], "client_order_id": request.client_order_id}))
    fence.release()
    lease.release()


def test_cold_restart_reconciles_durable_request_without_resubmission(tmp_path: Path) -> None:
    (tmp_path / "locks").mkdir()
    with _venue_server() as (base_url, truth):
        command = [sys.executable, "-B", "-u", __file__]
        first = subprocess.Popen(
            [*command, "a", str(tmp_path), base_url], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        first_out, first_err = first.communicate(timeout=10)
        assert first.returncode == -signal.SIGKILL, first_err
        assert first_out.strip() == "request-durable-before-crash"

        second = subprocess.run(
            [*command, "b", str(tmp_path), base_url], capture_output=True,
            text=True, timeout=10, check=False,
        )
        assert second.returncode == 0, second.stderr
        recovered = json.loads(second.stdout)
        assert recovered["decision"] == "hold"
        assert recovered["client_order_id"] == truth["client_order_id"]
        assert truth == {
            "client_order_id": recovered["client_order_id"],
            "submission_hits": 1, "status_hits": 1,
        }


if __name__ == "__main__":
    role, root_arg, url_arg = sys.argv[1:]
    {"a": _process_a, "b": _process_b}[role](Path(root_arg), url_arg)
