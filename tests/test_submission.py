from dataclasses import replace
from pathlib import Path

import pytest

from execution import orders
from execution.nonce import NonceAllocator, SignerFence
from execution.orders import (
    OrderContractError,
    ReconciliationEvidence,
    ReplayedDecisionHistory,
    make_order_intent,
    order_request_record,
)
from execution.writer import WriterIdentity, WriterLease, WriterLeaseError

ACCOUNT_DIGEST = "a" * 64
WALLET = "b" * 64
INSTANCE = "writer-one"


class InjectedWriterLease(WriterLease):
    pass


class InjectedNonceAllocator(NonceAllocator):
    pass


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


def _request(runtime, intent, **changes):
    lease, allocator, _ = runtime
    authority = lease.authority
    built = order_request_record(
        intent, 110, account_digest=allocator.account_digest,
        lease_epoch=authority.lease_epoch, writer_instance_id=INSTANCE,
        wallet_fingerprint=WALLET, allocated_nonce=501,
    )
    return replace(built, **changes)


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
    _, allocator, effects = submission_runtime
    intent = _intent()
    existing = _request(submission_runtime, intent)
    assert _submit(submission_runtime, intent, request=existing) == ("submit", "accepted")
    assert effects == [("transport", existing)]
    assert effects[0][1] is existing and allocator.last_nonce == 0


@pytest.mark.parametrize("field", ["account_digest", "wallet_fingerprint"])
def test_submit_resume_rejects_current_binding_mismatch(
    submission_runtime, field: str
) -> None:
    intent = _intent()
    existing = _request(submission_runtime, intent, **{field: "c" * 64})
    with pytest.raises(OrderContractError, match=f"resume request {field} mismatch"):
        _submit(submission_runtime, intent, request=existing)
    assert submission_runtime[2] == [] and submission_runtime[1].last_nonce == 0


def test_submit_resume_allows_prior_instance_and_epoch(submission_runtime) -> None:
    intent = _intent()
    existing = _request(
        submission_runtime, intent, writer_instance_id="prior-writer", lease_epoch=99
    )
    assert _submit(submission_runtime, intent, request=existing) == ("submit", "accepted")
    assert submission_runtime[2] == [("transport", existing)]


@pytest.mark.parametrize("mode", ["pending_reconciliation", "cancel_only"])
def test_persist_requires_current_submit_authority(submission_runtime, mode: str) -> None:
    lease, allocator, effects = submission_runtime
    lease._authority = lease.authority._replace(mode=mode)
    with pytest.raises(WriterLeaseError, match="action not authorized"):
        _submit(submission_runtime, _intent())
    assert effects == [] and allocator.last_nonce == 0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("lease", object(), "lease must be WriterLease"),
        ("allocator", object(), "allocator must be NonceAllocator"),
        ("transport", None, "transport must be callable"),
        ("request_recorder", None, "request_recorder must be callable"),
    ],
)
def test_submission_dependencies_fail_preflight_without_effects(
    submission_runtime, field: str, value: object, message: str
) -> None:
    with pytest.raises(TypeError, match=f"^{message}$"):
        _submit(submission_runtime, _intent(), **{field: value})
    assert submission_runtime[2] == [] and submission_runtime[1].last_nonce == 0


def test_submission_preflight_accepts_real_control_subclasses(submission_runtime) -> None:
    lease, allocator, _ = submission_runtime
    lease.__class__ = InjectedWriterLease
    allocator.__class__ = InjectedNonceAllocator
    assert _submit(submission_runtime, _intent()) == ("persist", "accepted")


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("now_ns", True, TypeError), ("now_ns", 0, ValueError),
        ("max_signal_age_ns", True, TypeError), ("max_signal_age_ns", 0, ValueError),
        ("max_reconcile_attempts", True, TypeError),
        ("max_reconcile_attempts", 0, ValueError),
        ("now_ms", True, TypeError), ("now_ms", 0, ValueError),
        ("decided_ns", True, TypeError), ("decided_ns", 0, ValueError),
    ],
)
def test_submission_scalars_fail_preflight_without_effects(
    submission_runtime, field: str, value: object, error: type[Exception]
) -> None:
    with pytest.raises(error, match=field):
        _submit(submission_runtime, _intent(), **{field: value})
    assert submission_runtime[2] == [] and submission_runtime[1].last_nonce == 0


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
