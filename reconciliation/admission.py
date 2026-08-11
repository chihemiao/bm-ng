"""Continuous fail-closed admission from observed runtime risk state."""

from reconciliation.clock import StateClock
from reconciliation.exposure import ExposureClock
from reconciliation.legs import PairState
from reconciliation.state import AdmissionDecision

EXPOSURE_STATES = frozenset({"flat", "naked", "unknown"})
PAIR_STATES = frozenset({"balanced", "unfilled", "imbalanced", "overfilled", "unknown"})
WALLET_STATUSES = frozenset({"active", "rotation_due", "expired"})


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


def _exposure_reasons(exposure: ExposureClock | None) -> list[str]:
    if exposure is None:
        return ["continuous_admission:exposure_unobserved"]
    reasons = []
    if exposure.state == "unknown":
        reasons.append("continuous_admission:exposure_unknown")
    if exposure.duration_exceeded is True:
        reasons.append("continuous_admission:naked_duration_exceeded")
    elif exposure.duration_exceeded is None:
        reasons.append("continuous_admission:naked_duration_unknown")
    return reasons


def _obligation_reasons(obligation: StateClock | None) -> list[str]:
    if obligation is None:
        return ["continuous_admission:obligation_unobserved"]
    if obligation.duration_exceeded is True:
        return ["continuous_admission:obligation_duration_exceeded"]
    if obligation.duration_exceeded is None:
        return ["continuous_admission:obligation_duration_unknown"]
    return []


def decide_continuous_admission(
    *,
    exposure: ExposureClock | None,
    obligation: StateClock | None,
    pair: PairState,
    agent_wallet_status: str,
    nonce_freeze_reason: str | None,
) -> AdmissionDecision:
    """Allow new risk only when every observed runtime condition is safe."""
    _validate_inputs(exposure, obligation, pair, agent_wallet_status, nonce_freeze_reason)
    reasons = [*_exposure_reasons(exposure), *_obligation_reasons(obligation)]
    if pair.state == "unknown":
        reasons.append("continuous_admission:pair_unknown")
    if agent_wallet_status == "expired":
        reasons.append("continuous_admission:agent_wallet_expired")
    if nonce_freeze_reason is not None:
        reasons.append(f"continuous_admission:nonce_frozen:{nonce_freeze_reason}")
    ordered = tuple(sorted(set(reasons)))
    action = "cancel_only_freeze" if ordered else "ready"
    return AdmissionDecision(action, ordered)
