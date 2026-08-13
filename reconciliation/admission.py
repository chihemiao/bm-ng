"""Continuous fail-closed admission from observed runtime risk state."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from execution.nonce import replay_freeze_reason
from execution.wallet import AgentWalletRegistration, WalletAssessment, assess
from reconciliation.clock import StateClock
from reconciliation.exposure import ExposureClock, advance_exposure_clock, delta_state
from reconciliation.fx import Notional
from reconciliation.legs import PairState, advance_obligation_clock
from reconciliation.state import AdmissionDecision

EXPOSURE_STATES = frozenset({"flat", "naked", "unknown"})
PAIR_STATES = frozenset({"balanced", "unfilled", "imbalanced", "overfilled", "unknown"})
WALLET_STATUSES = frozenset({"active", "rotation_due", "expired"})
CONTINUOUS_ADMISSION_REASONS = {
    name: f"continuous_admission:{name}"
    for name in (
        "exposure_unobserved",
        "exposure_unknown",
        "naked_duration_exceeded",
        "naked_duration_unknown",
        "obligation_unobserved",
        "obligation_duration_exceeded",
        "obligation_duration_unknown",
        "pair_unknown",
        "agent_wallet_expired",
        "nonce_frozen",
        "notional_exceeded",
        "notional_unknown",
    )
}
CONTINUOUS_ADMISSION_REASON_KEYS = frozenset(CONTINUOUS_ADMISSION_REASONS.values())


@dataclass(frozen=True, slots=True, kw_only=True)
class AdmissionSnapshotInputs:
    delta: Decimal | None
    previous_exposure: ExposureClock | None
    delta_tolerance: Decimal
    max_naked_ns: int
    pair: PairState
    previous_obligation: StateClock | None
    max_outstanding_ns: int
    registration: AgentWalletRegistration
    nonce_events: Sequence[Mapping[str, object]]
    naked_notional: Notional | None
    max_naked_notional: Notional
    observed_ns: int
    now_ns: int


@dataclass(frozen=True, slots=True, kw_only=True)
class DerivedAdmissionState:
    exposure: ExposureClock
    obligation: StateClock
    agent_wallet_status: WalletAssessment
    nonce_freeze_reason: str | None


def _validate_clock(value: object, expected: type, states: frozenset[str], name: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{name} has invalid type")
    if type(value.state) is not str or value.state not in states:
        raise ValueError(f"{name} has invalid state")
    exceeded = value.duration_exceeded
    if exceeded is not None and type(exceeded) is not bool:
        raise TypeError(f"{name} has invalid duration_exceeded")


def _validate_inputs(
    exposure: ExposureClock | None,
    obligation: StateClock | None,
    pair: PairState,
    wallet_status: str,
    nonce_reason: str | None,
    naked: Notional | None,
    maximum: Notional,
) -> None:
    if exposure is not None:
        _validate_clock(exposure, ExposureClock, EXPOSURE_STATES, "exposure")
    if obligation is not None:
        _validate_clock(obligation, StateClock, frozenset({"inactive", "active"}), "obligation")
    if not isinstance(pair, PairState):
        raise TypeError("pair must be PairState")
    if type(pair.state) is not str or pair.state not in PAIR_STATES:
        raise ValueError("pair has invalid state")
    if type(wallet_status) is not str:
        raise TypeError("agent_wallet_status must be a string")
    if wallet_status not in WALLET_STATUSES:
        raise ValueError("agent_wallet_status is invalid")
    if nonce_reason is not None and (type(nonce_reason) is not str or not nonce_reason):
        raise TypeError("nonce_freeze_reason must be a nonempty string or None")
    _validate_notionals(naked, maximum)


def _validate_notionals(naked: Notional | None, maximum: Notional) -> None:
    if naked is not None and not isinstance(naked, Notional):
        raise TypeError("naked_notional must be Notional or None")
    if not isinstance(maximum, Notional):
        raise TypeError("max_naked_notional must be Notional")
    if naked is not None and naked.quote != maximum.quote:
        raise ValueError("notional quote mismatch")


def _exposure_reasons(exposure: ExposureClock | None) -> list[str]:
    if exposure is None:
        return [CONTINUOUS_ADMISSION_REASONS["exposure_unobserved"]]
    reasons = []
    if exposure.state == "unknown":
        reasons.append(CONTINUOUS_ADMISSION_REASONS["exposure_unknown"])
    if exposure.duration_exceeded is True:
        reasons.append(CONTINUOUS_ADMISSION_REASONS["naked_duration_exceeded"])
    elif exposure.duration_exceeded is None:
        reasons.append(CONTINUOUS_ADMISSION_REASONS["naked_duration_unknown"])
    return reasons


def _obligation_reasons(obligation: StateClock | None) -> list[str]:
    if obligation is None:
        return [CONTINUOUS_ADMISSION_REASONS["obligation_unobserved"]]
    if obligation.duration_exceeded is True:
        return [CONTINUOUS_ADMISSION_REASONS["obligation_duration_exceeded"]]
    if obligation.duration_exceeded is None:
        return [CONTINUOUS_ADMISSION_REASONS["obligation_duration_unknown"]]
    return []


def _notional_reasons(naked: Notional | None, maximum: Notional) -> list[str]:
    if naked is None:
        return [CONTINUOUS_ADMISSION_REASONS["notional_unknown"]]
    if naked.amount > maximum.amount:
        return [CONTINUOUS_ADMISSION_REASONS["notional_exceeded"]]
    return []


def decide_continuous_admission(
    *,
    exposure: ExposureClock | None,
    obligation: StateClock | None,
    pair: PairState,
    agent_wallet_status: str,
    nonce_freeze_reason: str | None,
    naked_notional: Notional | None,
    max_naked_notional: Notional,
) -> AdmissionDecision:
    """Allow new risk only when every observed runtime condition is safe."""
    _validate_inputs(
        exposure,
        obligation,
        pair,
        agent_wallet_status,
        nonce_freeze_reason,
        naked_notional,
        max_naked_notional,
    )
    reasons = [
        *_exposure_reasons(exposure),
        *_obligation_reasons(obligation),
        *_notional_reasons(naked_notional, max_naked_notional),
    ]
    if pair.state == "unknown":
        reasons.append(CONTINUOUS_ADMISSION_REASONS["pair_unknown"])
    if agent_wallet_status == "expired":
        reasons.append(CONTINUOUS_ADMISSION_REASONS["agent_wallet_expired"])
    if nonce_freeze_reason is not None:
        reasons.append(f"{CONTINUOUS_ADMISSION_REASONS['nonce_frozen']}:{nonce_freeze_reason}")
    ordered = tuple(sorted(set(reasons)))
    action = "cancel_only_freeze" if ordered else "ready"
    return AdmissionDecision(action, ordered)


def build_continuous_admission_inputs(
    inputs: AdmissionSnapshotInputs,
) -> DerivedAdmissionState:
    """Derive the four observation-dependent admission values once."""
    if not isinstance(inputs, AdmissionSnapshotInputs):
        raise TypeError("inputs must be AdmissionSnapshotInputs")
    state = delta_state(inputs.delta, tolerance=inputs.delta_tolerance)
    exposure = advance_exposure_clock(
        inputs.previous_exposure,
        state=state,
        observed_ns=inputs.observed_ns,
        max_naked_ns=inputs.max_naked_ns,
    )
    obligation = advance_obligation_clock(
        inputs.previous_obligation,
        pair=inputs.pair,
        observed_ns=inputs.observed_ns,
        max_outstanding_ns=inputs.max_outstanding_ns,
    )
    wallet_status = assess(inputs.registration, inputs.now_ns)
    nonce_reason = replay_freeze_reason(
        inputs.nonce_events, inputs.registration.wallet_fingerprint
    )
    return DerivedAdmissionState(
        exposure=exposure,
        obligation=obligation,
        agent_wallet_status=wallet_status,
        nonce_freeze_reason=nonce_reason,
    )


def build_admission_snapshot(inputs: AdmissionSnapshotInputs) -> AdmissionDecision:
    """Advance this observation's clocks and derive one continuous decision."""
    derived = build_continuous_admission_inputs(inputs)
    return decide_continuous_admission(
        exposure=derived.exposure,
        obligation=derived.obligation,
        pair=inputs.pair,
        agent_wallet_status=derived.agent_wallet_status,
        nonce_freeze_reason=derived.nonce_freeze_reason,
        naked_notional=inputs.naked_notional,
        max_naked_notional=inputs.max_naked_notional,
    )
