"""Authorize, persist, and transport fail-closed order submissions."""

from collections.abc import Callable

from data.schema_nonce import signer_nonce_window_bounds
from execution.nonce import NonceAllocator
from execution.orders import (
    OrderIntent,
    OrderRequestRecord,
    ReconciliationEvidence,
    ReplayedDecisionHistory,
    _require,
    decide_submission,
    order_request_record,
)
from execution.writer import WriterLease


def _validate_submission_inputs(
    lease: object,
    allocator: object,
    transport: object,
    request_recorder: object,
    scalars: tuple[tuple[str, object], ...],
) -> None:
    if not isinstance(lease, WriterLease):
        raise TypeError("lease must be WriterLease")
    if not isinstance(allocator, NonceAllocator):
        raise TypeError("allocator must be NonceAllocator")
    if not callable(transport):
        raise TypeError("transport must be callable")
    if not callable(request_recorder):
        raise TypeError("request_recorder must be callable")
    for name, value in scalars:
        if type(value) is not int:
            raise TypeError(f"{name} must be an integer")
        if value <= 0:
            raise ValueError(f"{name} must be positive")


def _validate_resume_request(
    intent: OrderIntent,
    request: OrderRequestRecord,
    allocator: NonceAllocator,
    wallet_fingerprint: str,
    now_ms: int,
) -> None:
    _require(
        request.account_digest == allocator.account_digest,
        "resume request account_digest mismatch",
    )
    _require(
        request.wallet_fingerprint == wallet_fingerprint,
        "resume request wallet_fingerprint mismatch",
    )
    if intent.leg == "hyperliquid":
        nonce = request.allocated_nonce
        assert nonce is not None  # Guaranteed by order request validation.
        window = signer_nonce_window_bounds(nonce=nonce, now_ms=now_ms)
        _require(
            window.lower_ok,
            "resumed request nonce is not strictly after now_ms minus two days",
        )
        _require(
            window.upper_ok,
            "resumed request nonce is not strictly before now_ms plus one day",
        )


def submit_order(
    intent: OrderIntent, evidence: ReconciliationEvidence,
    request: OrderRequestRecord | None, history: ReplayedDecisionHistory,
    lease: WriterLease, allocator: NonceAllocator,
    transport: Callable[[OrderRequestRecord], object],
    request_recorder: Callable[[OrderRequestRecord], None],
    *, now_ns: int, max_signal_age_ns: int, max_reconcile_attempts: int,
    now_ms: int, decided_ns: int,
) -> tuple[str, object | None]:
    _validate_submission_inputs(
        lease, allocator, transport, request_recorder,
        (
            ("now_ns", now_ns), ("max_signal_age_ns", max_signal_age_ns),
            ("max_reconcile_attempts", max_reconcile_attempts),
            ("now_ms", now_ms), ("decided_ns", decided_ns),
        ),
    )
    decision = decide_submission(
        intent, evidence, request, history, now_ns,
        max_signal_age_ns, max_reconcile_attempts,
    )
    if decision not in {"persist", "submit"}:
        return decision, None
    authority = lease.authorize("submit")
    if decision == "submit":
        assert request is not None  # Guaranteed by decide_submission.
        _validate_resume_request(
            intent, request, allocator, authority.identity.wallet_fingerprint, now_ms,
        )
        return decision, transport(request)
    nonce = None
    if intent.leg == "hyperliquid":
        nonce = allocator.allocate(now_ms=now_ms, decided_ns=decided_ns)
    identity = authority.identity
    built = order_request_record(
        intent, decided_ns, account_digest=allocator.account_digest,
        lease_epoch=authority.lease_epoch, writer_instance_id=identity.instance_id,
        wallet_fingerprint=identity.wallet_fingerprint, allocated_nonce=nonce,
    )
    request_recorder(built)
    return decision, transport(built)
