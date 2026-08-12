from decimal import Decimal
from pathlib import Path

import pytest

import reconciliation.promotion as promotion
from execution.nonce import NonceAllocator, SignerFence
from execution.orders import (
    ReconciliationEvidence,
    ReplayedDecisionHistory,
    make_order_intent,
    submit_order,
)
from execution.writer import WriterIdentity, WriterLease, WriterLeaseError
from reconciliation.admission import decide_continuous_admission
from reconciliation.clock import StateClock
from reconciliation.exposure import ExposureClock
from reconciliation.fx import Notional
from reconciliation.legs import PairState
from reconciliation.promotion import demote_writer, promote_writer
from reconciliation.state import AdmissionDecision

ACCOUNT_DIGEST = "a" * 64
WALLET = "b" * 64
INSTANCE = "writer-one"


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
    intent = make_order_intent("funding-carry", "git-deadbeef", 100, "hyperliquid")

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


def test_continuous_admission_has_no_periodic_runtime_caller_yet() -> None:
    runtime_roots = ("data", "execution", "reconciliation")
    calls = {
        str(path): path.read_text().count("apply_continuous_admission(")
        for root in runtime_roots
        for path in Path(root).glob("*.py")
        if "apply_continuous_admission(" in path.read_text()
    }
    assert calls == {"reconciliation/promotion.py": 1}
