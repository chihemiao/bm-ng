"""Authorize, persist, and transport fail-closed order submissions."""

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Literal

from data.schema_nonce import signer_nonce_window_bounds
from execution.nonce import NonceAllocator
from execution.order_serde import serialize_order_observation
from execution.orders import (
    FlattenIntentPlan,
    OrderIntent,
    OrderRequestRecord,
    ReconciliationEvidence,
    ReplayedDecisionHistory,
    T0APairIntents,
    _require,
    decide_submission,
    next_flatten_intent,
    order_request_record,
    t0a_pair_intents_match,
)
from execution.writer import WriterLease


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservedFields:
    venue_order_id: str | None
    status: str
    observation_source: str
    venue_time_ms: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class PairLegSubmissionInputs:
    evidence: ReconciliationEvidence
    request: OrderRequestRecord | None
    history: ReplayedDecisionHistory
    transport: Callable[[OrderRequestRecord], object]


@dataclass(frozen=True, slots=True, kw_only=True)
class PairSubmissionOutcome:
    hyperliquid: object | BaseException
    bybit: object | BaseException


@dataclass(frozen=True, slots=True, kw_only=True)
class FlattenStepOutcome:
    intent: OrderIntent
    result: tuple[str, object | None]


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
    def event(request: OrderRequestRecord, observed: ObservedFields) -> Mapping[str, object]:
        return serialize_order_observation(**asdict(observed),
            venue=request.leg, client_order_id=request.client_order_id,
            observed_ns=observed_ns, conn_id=conn_id, boot_id=boot_id,
            recv_wall_ns=recv_wall_ns, recv_mono_ns=recv_mono_ns, source=source,
            seq_within_boot=seq_within_boot)

    def record(observation: Mapping[str, object], *, outcome: Literal["success", "failure"],
               result: object | None, transport_error: BaseException | None) -> None:
        try:
            observation_recorder(observation)
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
            record(event(request, unknown), outcome="failure", result=None,
                   transport_error=transport_error)
            raise
        try:
            observed = success_mapper(result, request)
            if not isinstance(observed, ObservedFields):
                raise TypeError("success_mapper must return ObservedFields")
            observation = event(request, observed)
        except Exception as error:
            raise ObservationMappingError(result) from error
        record(observation, outcome="success", result=result, transport_error=None)
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
    authority = lease.authorize("reduce_only" if intent.reduce_only else "submit")
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


def submit_flatten_step(
    plan: FlattenIntentPlan,
    hyperliquid_input: object,
    bybit_input: object,
    *,
    lease: WriterLease,
    allocator: NonceAllocator,
    request_recorder: Callable[[OrderRequestRecord], None],
    now_ns: int,
    max_signal_age_ns: int,
    max_reconcile_attempts: int,
    now_ms: int,
    decided_ns: int,
) -> FlattenStepOutcome | None:
    """Submit at most one risk-minimizing intent from an authoritative plan."""
    intent = next_flatten_intent(plan)
    if intent is None:
        return None
    selected = hyperliquid_input if intent.leg == "hyperliquid" else bybit_input
    if not isinstance(selected, PairLegSubmissionInputs):
        raise TypeError("selected input must be PairLegSubmissionInputs")
    result = submit_order(
        intent, selected.evidence, selected.request, selected.history,
        lease, allocator, selected.transport, request_recorder,
        now_ns=now_ns, max_signal_age_ns=max_signal_age_ns,
        max_reconcile_attempts=max_reconcile_attempts,
        now_ms=now_ms, decided_ns=decided_ns,
    )
    return FlattenStepOutcome(intent=intent, result=result)


def submit_t0a_pair(
    *, pair: T0APairIntents, hyperliquid: PairLegSubmissionInputs,
    bybit: PairLegSubmissionInputs, lease: WriterLease, allocator: NonceAllocator,
    request_recorder: Callable[[OrderRequestRecord], None], now_ns: int,
    max_signal_age_ns: int, max_reconcile_attempts: int, now_ms: int, decided_ns: int,
) -> PairSubmissionOutcome:
    """Submit each T0A leg once in deterministic order and preserve both outcomes."""
    if not isinstance(pair, T0APairIntents):
        raise TypeError("pair must be T0APairIntents")
    if not t0a_pair_intents_match(pair):
        raise ValueError("pair intents do not match T0A topology")

    def run(intent: OrderIntent, values: PairLegSubmissionInputs) -> object | BaseException:
        try:
            return submit_order(
                intent, values.evidence, values.request, values.history, lease, allocator,
                values.transport, request_recorder, now_ns=now_ns,
                max_signal_age_ns=max_signal_age_ns,
                max_reconcile_attempts=max_reconcile_attempts, now_ms=now_ms,
                decided_ns=decided_ns,
            )
        except BaseException as error:
            return error

    return PairSubmissionOutcome(
        hyperliquid=run(pair.hyperliquid, hyperliquid),
        bybit=run(pair.bybit, bybit),
    )
