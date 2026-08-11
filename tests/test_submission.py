from pathlib import Path

import pytest

from execution import orders
from execution.nonce import NonceAllocator, SignerFence
from execution.orders import (
    ReconciliationEvidence,
    ReplayedDecisionHistory,
    make_order_intent,
    order_request_record,
)
from execution.writer import WriterIdentity, WriterLease

ACCOUNT_DIGEST = "a" * 64
WALLET = "b" * 64
INSTANCE = "writer-one"


def _intent(leg: str = "hyperliquid"):
    return make_order_intent("funding-carry", "git-deadbeef", 100, leg)


def _evidence(status: str = "absent") -> ReconciliationEvidence:
    return ReconciliationEvidence(status, 101, 102, 103)


def _history(intent, *, frozen: bool = False) -> ReplayedDecisionHistory:
    return ReplayedDecisionHistory(intent.client_order_id, 0, frozen)


@pytest.fixture
def submission_runtime(tmp_path: Path):
    identity = WriterIdentity("test-account", INSTANCE, WALLET, "boot-one")
    lease = WriterLease.acquire(tmp_path, identity, [].append, acquired_ns=90)
    lease._authority = lease.authority._replace(mode="risk_increasing")
    fence = SignerFence.acquire(tmp_path, WALLET, INSTANCE)
    effects = []

    def record_nonce(payload) -> None:
        effects.append(("nonce", payload))

    allocator = NonceAllocator(
        fence, lease, account_digest=ACCOUNT_DIGEST, replayed_last=0,
        replayed_freeze_reason=None, recorder=record_nonce,
    )
    yield lease, allocator, effects
    fence.release()
    lease.release()


def _submit(runtime, intent, **changes):
    lease, allocator, effects = runtime

    def record_request(request) -> None:
        effects.append(("request", request))

    def transport(request):
        effects.append(("transport", request))
        return "accepted"

    values = {
        "intent": intent, "evidence": _evidence(), "request": None,
        "history": _history(intent), "lease": lease, "allocator": allocator,
        "transport": transport, "request_recorder": record_request,
        "now_ns": 120, "max_signal_age_ns": 50, "max_reconcile_attempts": 3,
        "now_ms": 500, "decided_ns": 110,
    }
    values.update(changes)
    return orders.submit_order(**values)


def test_hyperliquid_persist_allocates_records_then_transports(submission_runtime) -> None:
    lease, allocator, effects = submission_runtime
    assert _submit(submission_runtime, _intent()) == ("persist", "accepted")
    assert [kind for kind, _ in effects] == ["nonce", "request", "transport"]

    nonce_payload, built, sent = (entry[1] for entry in effects)
    authority = lease.authority
    assert built.account_digest == allocator.account_digest == ACCOUNT_DIGEST
    assert built.lease_epoch == authority.lease_epoch
    assert built.writer_instance_id == authority.identity.instance_id
    assert built.wallet_fingerprint == authority.identity.wallet_fingerprint
    assert built.allocated_nonce == nonce_payload["allocated_nonce"] == 501
    assert built.recorded_ns == 110 and sent is built


def test_bybit_persist_skips_nonce_but_uses_the_bound_account(submission_runtime) -> None:
    _, allocator, effects = submission_runtime
    assert _submit(submission_runtime, _intent("bybit")) == ("persist", "accepted")
    assert [kind for kind, _ in effects] == ["request", "transport"]
    built, sent = (entry[1] for entry in effects)
    assert built.account_digest == allocator.account_digest
    assert built.allocated_nonce is None and allocator.last_nonce == 0
    assert sent is built


def test_submit_resume_transports_the_existing_request_only(submission_runtime) -> None:
    lease, allocator, effects = submission_runtime
    intent = _intent()
    authority = lease.authority
    existing = order_request_record(
        intent, 110, account_digest=allocator.account_digest,
        lease_epoch=authority.lease_epoch, writer_instance_id=INSTANCE,
        wallet_fingerprint=WALLET, allocated_nonce=501,
    )
    assert _submit(submission_runtime, intent, request=existing) == ("submit", "accepted")
    assert effects == [("transport", existing)]
    assert effects[0][1] is existing and allocator.last_nonce == 0


@pytest.mark.parametrize("decision", ["freeze", "reconcile", "hold", "reject_stale"])
def test_non_submission_decisions_have_zero_side_effects(
    submission_runtime, decision: str
) -> None:
    intent = _intent()
    changes = {}
    if decision == "freeze":
        changes["history"] = _history(intent, frozen=True)
    elif decision == "reconcile":
        changes["evidence"] = _evidence("pending")
    elif decision == "hold":
        changes["evidence"] = _evidence("open")
    else:
        changes["now_ns"] = 151
    assert _submit(submission_runtime, intent, **changes) == (decision, None)
    assert submission_runtime[2] == [] and submission_runtime[1].last_nonce == 0
