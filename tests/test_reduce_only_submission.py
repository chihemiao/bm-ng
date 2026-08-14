from contextlib import contextmanager
from decimal import Decimal

import pytest

from execution.nonce import NonceAllocator, SignerFence
from execution.orders import (
    ReconciliationEvidence,
    ReplayedDecisionHistory,
    make_order_intent,
)
from execution.submission import submit_order
from execution.writer import WriterIdentity, WriterLease, WriterLeaseError

ACCOUNT_DIGEST = "a" * 64
WALLET = "b" * 64
INSTANCE = "writer-one"


def _intent(*, reduce_only: bool):
    return make_order_intent(
        "funding-carry",
        "git-deadbeef",
        100,
        "hyperliquid",
        symbol="BTC",
        side="buy",
        quantity=Decimal("1"),
        reduce_only=reduce_only,
    )


@contextmanager
def _runtime(root, mode):
    decisions, nonce_events, effects = [], [], []
    identity = WriterIdentity("test-account", INSTANCE, WALLET, "boot-one")
    lease = WriterLease.acquire(root, identity, decisions.append, acquired_ns=90)
    lease._authority = lease.authority._replace(mode=mode)
    fence = SignerFence.acquire(root, WALLET, INSTANCE)
    allocator = NonceAllocator(
        fence,
        lease,
        account_digest=ACCOUNT_DIGEST,
        replayed_last=0,
        replayed_freeze_reason=None,
        recorder=nonce_events.append,
    )
    try:
        yield lease, allocator, decisions, nonce_events, effects
    finally:
        fence.release()
        lease.release()


def _submit(runtime, intent, *, request=None):
    lease, allocator, _, _, effects = runtime

    def record_request(value):
        effects.append(("request", value))

    def transport(value):
        effects.append(("transport", value))
        return "accepted"

    return submit_order(
        intent,
        ReconciliationEvidence("absent", 111, 112, 113),
        request,
        ReplayedDecisionHistory(intent.client_order_id, 0, False),
        lease,
        allocator,
        transport,
        record_request,
        now_ns=120,
        max_signal_age_ns=50,
        max_reconcile_attempts=3,
        now_ms=500,
        decided_ns=110,
    )


@pytest.mark.parametrize(
    ("mode", "reduce_only", "requested_action"),
    [
        ("cancel_only", False, "submit"),
        ("cancel_only", True, "reduce_only"),
        ("pending_reconciliation", True, "reduce_only"),
    ],
)
def test_denial_evidence_names_the_intent_authorization_action(
    tmp_path,
    mode,
    reduce_only,
    requested_action,
):
    with _runtime(tmp_path, mode) as runtime:
        lease, allocator, decisions, nonce_events, effects = runtime
        with pytest.raises(WriterLeaseError, match="action not authorized"):
            _submit(runtime, _intent(reduce_only=reduce_only))
        assert decisions[-1].reason == (
            f"authorize_denied:{mode}:{requested_action}:action_not_authorized"
        )
        assert lease.authority.mode == mode
        assert allocator.last_nonce == 0
        assert nonce_events == [] and effects == []


@pytest.mark.parametrize("reduce_only", [False, True])
def test_risk_increasing_authority_preserves_both_persist_paths(
    tmp_path,
    reduce_only,
):
    with _runtime(tmp_path, "risk_increasing") as runtime:
        result = _submit(runtime, _intent(reduce_only=reduce_only))
        assert result == ("persist", "accepted")
        assert [kind for kind, _ in runtime[-1]] == ["request", "transport"]
        assert runtime[1].last_nonce == 501


def test_reduce_only_resume_uses_the_same_authorization_mapping(tmp_path):
    with _runtime(tmp_path, "risk_increasing") as runtime:
        lease, allocator, decisions, nonce_events, effects = runtime
        intent = _intent(reduce_only=True)
        assert _submit(runtime, intent) == ("persist", "accepted")
        request = next(value for kind, value in effects if kind == "request")
        lease.demote_to_cancel_only(demotion_ns=121, reason="writer_demoted:test")
        before = (allocator.last_nonce, len(nonce_events), len(decisions))
        effects.clear()

        with pytest.raises(WriterLeaseError, match="action not authorized"):
            _submit(runtime, intent, request=request)
        assert decisions[-1].reason == (
            "authorize_denied:cancel_only:reduce_only:action_not_authorized"
        )
        assert (allocator.last_nonce, len(nonce_events), len(decisions)) == (
            before[0],
            before[1],
            before[2] + 1,
        )
        assert effects == []
