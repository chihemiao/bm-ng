import ast
import inspect
import json
import socket
import threading
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

import reconciliation.admission as admission
import reconciliation.promotion as promotion
from data import shard
from data.contracts import VALIDITY_NS
from execution.nonce import NonceAllocator, SignerFence
from execution.order_serde import rehydrate_order_request, serialize_order_request
from execution.orders import (
    ReconciliationEvidence,
    ReplayedDecisionHistory,
    make_order_intent,
)
from execution.submission import submit_order
from execution.wallet import AgentWalletRegistration
from execution.writer import WriterIdentity, WriterLease, WriterLeaseError
from reconciliation.admission import (
    AdmissionSnapshotInputs,
    DerivedAdmissionState,
    build_continuous_admission_inputs,
    decide_continuous_admission,
)
from reconciliation.clock import StateClock
from reconciliation.exposure import ExposureClock
from reconciliation.fx import Notional
from reconciliation.legs import PairState, build_order_reconciliation_evidence
from reconciliation.promotion import demote_writer, promote_writer, run_admission_cycle
from reconciliation.state import AdmissionDecision

ACCOUNT_DIGEST = "a" * 64
WALLET = "b" * 64
INSTANCE = "writer-one"


@contextmanager
def _ack_loss_server():
    truth = {"client_order_id": None, "submission_hits": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            truth["client_order_id"] = json.loads(body)["client_order_id"]
            truth["submission_hits"] += 1
            self.connection.shutdown(socket.SHUT_RDWR)

        def do_GET(self):
            body = json.dumps(
                {"client_order_id": truth["client_order_id"],
                 "status": "filled", "observed_ns": 111}
            ).encode()
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


def _order_status_event(observed):
    return {
        "schema_ver": 1, "event_kind": "order", "payload_schema": "order_observation",
        "venue": "hyperliquid", "conn_id": "local-http", "boot_id": "boot-observation",
        "recv_wall_ns": 112, "recv_mono_ns": 112, "source": "order_status",
        "seq_within_boot": 1, "identity_status": "known",
        "client_order_id": observed["client_order_id"], "venue_order_id": "7",
        "payload": {"status": observed["status"], "source": "order_status",
                    "observed_ns": observed["observed_ns"], "venue_time_ms": 1},
    }


def _continuous_inputs(**changes: object) -> dict[str, object]:
    values = {
        "exposure": ExposureClock("flat", 100, None, False),
        "obligation": StateClock("inactive", 100, None, False),
        "pair": PairState("balanced", ()),
        "agent_wallet_status": "active",
        "nonce_freeze_reason": None,
        "naked_notional": Notional(Decimal(0), "USDC"),
        "max_naked_notional": Notional(Decimal("1000"), "USDC"),
    }
    values.update(changes)
    return values


def _continuous(**changes: object) -> AdmissionDecision:
    return decide_continuous_admission(**_continuous_inputs(**changes))  # type: ignore[arg-type]


def _snapshot(**changes: object) -> AdmissionSnapshotInputs:
    values = {
        "delta": Decimal(0),
        "previous_exposure": ExposureClock("flat", 100, None, False),
        "delta_tolerance": Decimal("0.001"),
        "max_naked_ns": 10,
        "pair": PairState("balanced", ()),
        "previous_obligation": StateClock("inactive", 100, None, False),
        "max_outstanding_ns": 10,
        "registration": AgentWalletRegistration(WALLET, 1, 1 + VALIDITY_NS),
        "nonce_events": (),
        "naked_notional": Notional(Decimal(0), "USDC"),
        "max_naked_notional": Notional(Decimal("1000"), "USDC"),
        "observed_ns": 105,
        "now_ns": 105,
    }
    values.update(changes)
    return AdmissionSnapshotInputs(**values)


@pytest.fixture
def authorized_runtime(tmp_path: Path):
    writer_events = []
    identity = WriterIdentity("hyperliquid:test", INSTANCE, WALLET, "boot-one")
    lease = WriterLease.acquire(tmp_path, identity, writer_events.append, acquired_ns=90)
    promotions = []
    authority = promote_writer(lease, _continuous(), promotions.append, now_ns=91)
    assert authority.mode == "risk_increasing"
    assert len(promotions) == 1 and promotions[0].outcome == "promoted"
    writer_events.clear()

    fence = SignerFence.acquire(tmp_path, WALLET, INSTANCE)
    effects = []
    allocator = NonceAllocator(
        fence,
        lease,
        account_digest=ACCOUNT_DIGEST,
        replayed_last=0,
        replayed_freeze_reason=None,
        recorder=lambda row: effects.append(("nonce", row)),
    )
    yield lease, allocator, writer_events, effects
    fence.release()
    lease.release()


def _submit(runtime):
    lease, allocator, _, effects = runtime
    intent = make_order_intent(
        "funding-carry", "git-deadbeef", 100, "hyperliquid",
        symbol="BTC", side="buy", quantity=Decimal("1"),
    )

    def transport(request):
        effects.append(("transport", request))
        return "accepted"

    return submit_order(
        intent,
        ReconciliationEvidence("absent", 101, 102, 103),
        None,
        ReplayedDecisionHistory(intent.client_order_id, 0, False),
        lease,
        allocator,
        transport,
        lambda row: effects.append(("request", row)),
        now_ns=120,
        max_signal_age_ns=50,
        max_reconcile_attempts=3,
        now_ms=500,
        decided_ns=110,
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"exposure": ExposureClock("unknown", 100, None, False)},
            "continuous_admission:exposure_unknown",
        ),
        (
            {"exposure": ExposureClock("naked", 111, 100, True)},
            "continuous_admission:naked_duration_exceeded",
        ),
        (
            {"obligation": StateClock("active", 111, 100, True)},
            "continuous_admission:obligation_duration_exceeded",
        ),
        (
            {"naked_notional": Notional(Decimal("1001"), "USDC")},
            "continuous_admission:notional_exceeded",
        ),
        (
            {"nonce_freeze_reason": "signer_nonce:conflict"},
            "continuous_admission:nonce_frozen:signer_nonce:conflict",
        ),
    ],
)
def test_continuous_freeze_demotes_before_submission_side_effects(
    authorized_runtime, changes: dict, reason: str
) -> None:
    lease, allocator, writer_events, effects = authorized_runtime
    admission = _continuous(**changes)
    assert admission == AdmissionDecision("cancel_only_freeze", (reason,))

    demote_writer(lease, admission, now_ns=105)
    demotions = [row for row in writer_events if row.action == "demote"]
    assert len(demotions) == 1 and lease.authority.mode == "cancel_only"
    with pytest.raises(WriterLeaseError, match="not authorized"):
        _submit(authorized_runtime)
    assert effects == [] and allocator.last_nonce == 0


def test_ready_authority_records_nonce_request_then_transports_once(
    authorized_runtime,
) -> None:
    lease, allocator, writer_events, effects = authorized_runtime
    assert _continuous() == AdmissionDecision("ready", ())
    assert lease.authority.mode == "risk_increasing" and writer_events == []

    assert _submit(authorized_runtime) == ("persist", "accepted")
    assert [kind for kind, _ in effects] == ["nonce", "request", "transport"]
    assert allocator.last_nonce == 501


def test_real_ack_loss_never_retransports_before_venue_truth_catches_up(
    authorized_runtime, tmp_path: Path,
) -> None:
    lease, allocator, _, _ = authorized_runtime
    intent = make_order_intent(
        "funding-carry", "git-deadbeef", 100, "hyperliquid",
        symbol="BTC", side="buy", quantity=Decimal("1"),
    )
    event_root = tmp_path / "ack-loss-events"
    event_writer = shard.ShardWriter(event_root, boot_id="boot-orders")

    def record_request(record):
        event = serialize_order_request(
            intent, record, conn_id="local-http", boot_id="boot-orders",
            recv_wall_ns=110, recv_mono_ns=110, source="execution",
            seq_within_boot=1,
        )
        event_writer.append_event(event)
        event_writer.close()

    def call(evidence, request, transport):
        return submit_order(
            intent, evidence, request,
            ReplayedDecisionHistory(intent.client_order_id, 0, False),
            lease, allocator, transport, record_request,
            now_ns=120, max_signal_age_ns=50, max_reconcile_attempts=3,
            now_ms=500, decided_ns=110,
        )

    with _ack_loss_server() as (base_url, truth):
        def transport(record):
            body = json.dumps({"client_order_id": record.client_order_id}).encode()
            with urlopen(Request(base_url, data=body, method="POST"), timeout=1) as response:
                return response.read()

        old_absence = ReconciliationEvidence("absent", 101, 102, 103)
        with pytest.raises(OSError):
            call(old_absence, None, transport)
        replay = shard.replay_event_window(event_root, 0, 200)
        _, durable_request = rehydrate_order_request(replay.events[0])
        assert truth == {"client_order_id": intent.client_order_id, "submission_hits": 1}

        assert call(old_absence, durable_request, transport) == ("reconcile", None)
        assert truth["submission_hits"] == 1

        with urlopen(f"{base_url}/orderStatus", timeout=1) as response:
            observed = json.load(response)
        observation_writer = shard.ShardWriter(event_root, boot_id="boot-observation")
        observation_writer.append_event(_order_status_event(observed))
        observation_writer.close()
        replay = shard.replay_event_window(event_root, 0, 200)
        filled = build_order_reconciliation_evidence(
            replay, client_order_id=intent.client_order_id, venue="hyperliquid",
        )
        assert filled == ReconciliationEvidence("filled", 111, None, None)
        assert call(filled, durable_request, transport) == ("hold", None)
        assert truth["submission_hits"] == 1


def test_applied_continuous_freeze_prevents_transport(
    authorized_runtime,
) -> None:
    lease, allocator, writer_events, effects = authorized_runtime
    inputs = _continuous_inputs(exposure=ExposureClock("unknown", 100, None, False))
    admission = promotion.apply_continuous_admission(lease, **inputs, now_ns=105)
    assert admission.action == "cancel_only_freeze"
    assert lease.authority.mode == "cancel_only"
    assert [row.action for row in writer_events] == ["demote"]

    with pytest.raises(WriterLeaseError, match="not authorized"):
        _submit(authorized_runtime)
    assert effects == [] and allocator.last_nonce == 0


def test_derived_admission_state_is_the_named_enforcement_boundary() -> None:
    assert tuple(DerivedAdmissionState.__dataclass_fields__) == (
        "exposure", "obligation", "agent_wallet_status", "nonce_freeze_reason"
    )
    derived = build_continuous_admission_inputs(_snapshot())
    assert admission.build_admission_snapshot(_snapshot()) == decide_continuous_admission(
        exposure=derived.exposure,
        obligation=derived.obligation,
        pair=_snapshot().pair,
        agent_wallet_status=derived.agent_wallet_status,
        nonce_freeze_reason=derived.nonce_freeze_reason,
        naked_notional=_snapshot().naked_notional,
        max_naked_notional=_snapshot().max_naked_notional,
    )


def test_snapshot_and_cycle_share_one_derivation_path() -> None:
    snapshot_calls = {
        node.func.id for node in ast.walk(ast.parse(inspect.getsource(
            admission.build_admission_snapshot)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    cycle_calls = {
        node.func.id for node in ast.walk(ast.parse(inspect.getsource(
            promotion.run_admission_cycle)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "build_continuous_admission_inputs" in snapshot_calls & cycle_calls
    assert "apply_continuous_admission" in cycle_calls


def test_runtime_cycle_freeze_demotes_a_real_writer_lease(authorized_runtime) -> None:
    lease, _, writer_events, _ = authorized_runtime
    decision = run_admission_cycle(_snapshot(delta=None), lease)
    assert decision.action == "cancel_only_freeze"
    assert lease.authority.mode == "cancel_only"
    assert [event.action for event in writer_events] == ["demote"]


def test_runtime_cycle_ready_keeps_authority_and_has_no_effects(authorized_runtime) -> None:
    lease, _, writer_events, effects = authorized_runtime
    assert run_admission_cycle(_snapshot(), lease) == AdmissionDecision("ready", ())
    assert lease.authority.mode == "risk_increasing"
    assert writer_events == effects == []


def test_continuous_admission_has_a_runtime_cycle_caller() -> None:
    runtime_roots = ("data", "execution", "reconciliation")
    calls = {
        str(path): path.read_text().count("apply_continuous_admission(")
        for root in runtime_roots
        for path in Path(root).glob("*.py")
        if "apply_continuous_admission(" in path.read_text()
    }
    assert calls == {"reconciliation/promotion.py": 2}
