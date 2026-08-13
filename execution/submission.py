"""Authorize, persist, and transport fail-closed order submissions."""

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Literal

from data.schema_nonce import signer_nonce_window_bounds
from execution.nonce import NonceAllocator
from execution.order_serde import serialize_order_observation
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


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservedFields:
    venue_order_id: str | None
    status: str
    observation_source: str
    venue_time_ms: int | None


class ObservationMappingError(RuntimeError):
    def __init__(self, result: object) -> None:
        self.result = result
        super().__init__("transport result could not be mapped to an observation")


class ObservationRecordingError(RuntimeError):
    def __init__(self, *, outcome: Literal["success", "failure"], result: object | None,
                 transport_error: BaseException | None) -> None:
        self.outcome, self.result, self.transport_error = outcome, result, transport_error
        super().__init__(f"transport {outcome} occurred but observation recording failed")


def observe_transport(
    transport: Callable[[OrderRequestRecord], object], *,
    success_mapper: Callable[[object, OrderRequestRecord], ObservedFields],
    observation_recorder: Callable[[Mapping[str, object]], None], observed_ns: int,
    conn_id: str, boot_id: str, recv_wall_ns: int, recv_mono_ns: int, source: str,
    seq_within_boot: int,
) -> Callable[[OrderRequestRecord], object]:
    """Decorate an opaque transport with durable observation recording."""
    def record(request: OrderRequestRecord, observed: ObservedFields, *,
               outcome: Literal["success", "failure"], result: object | None,
               transport_error: BaseException | None) -> None:
        event = serialize_order_observation(**asdict(observed),
            venue=request.leg, client_order_id=request.client_order_id,
            observed_ns=observed_ns, conn_id=conn_id, boot_id=boot_id,
            recv_wall_ns=recv_wall_ns, recv_mono_ns=recv_mono_ns, source=source,
            seq_within_boot=seq_within_boot)
        try:
            observation_recorder(event)
        except Exception as error:
            raise ObservationRecordingError(outcome=outcome, result=result,
                                            transport_error=transport_error) from error

    def observed_transport(request: OrderRequestRecord) -> object:
        try:
            result = transport(request)
        except Exception as transport_error:
            unknown = ObservedFields(
                venue_order_id=None, status="unknown",
                observation_source="no_venue_response", venue_time_ms=None)
            record(request, unknown, outcome="failure", result=None,
                   transport_error=transport_error)
            raise
        try:
            observed = success_mapper(result, request)
            if not isinstance(observed, ObservedFields):
                raise TypeError("success_mapper must return ObservedFields")
        except Exception as error:
            raise ObservationMappingError(result) from error
        record(request, observed, outcome="success", result=result, transport_error=None)
        return result

    return observed_transport


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
