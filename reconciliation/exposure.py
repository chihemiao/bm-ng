"""Fail-closed cross-venue exposure arithmetic."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from reconciliation.clock import StateClock, advance_state_clock
from reconciliation.state import (
    VENUES,
    SurfaceEvidence,
    surface_is_authoritative,
    validate_surface_evidence,
)

EXPOSURE_STATES = {"flat": "inactive", "naked": "active", "unknown": "unknown"}
CORE_EXPOSURE_STATES = {value: key for key, value in EXPOSURE_STATES.items()}


@dataclass(frozen=True, slots=True)
class LegPosition:
    venue: str
    symbol: str
    signed_quantity: Decimal
    evidence: SurfaceEvidence


@dataclass(frozen=True, slots=True)
class ExposureClock:
    state: str
    observed_ns: int
    naked_since_ns: int | None
    duration_exceeded: bool | None


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _validate_leg(leg: LegPosition, symbol: str) -> None:
    if not isinstance(leg, LegPosition):
        raise TypeError("position must be LegPosition")
    if leg.symbol != symbol:
        raise ValueError("position symbol does not match requested symbol")
    if type(leg.signed_quantity) is not Decimal:
        raise TypeError("signed_quantity must be Decimal")
    if not leg.signed_quantity.is_finite():
        raise ValueError("signed_quantity must be finite")
    evidence = leg.evidence
    observed_ns = evidence.observed_ns if isinstance(evidence, SurfaceEvidence) else 0
    validate_surface_evidence(evidence, now_ns=observed_ns)


def delta_state(delta: Decimal | None, *, tolerance: Decimal) -> str:
    """Classify exact cross-leg exposure with a closed fail-safe state set."""
    if type(tolerance) is not Decimal:
        raise TypeError("tolerance must be Decimal")
    if not tolerance.is_finite():
        raise ValueError("tolerance must be finite")
    if tolerance < 0:
        raise ValueError("tolerance must be non-negative")
    if delta is None:
        return "unknown"
    if type(delta) is not Decimal:
        raise TypeError("delta must be Decimal or None")
    if not delta.is_finite():
        raise ValueError("delta must be finite")
    return "flat" if abs(delta) <= tolerance else "naked"


def _core_exposure_state(state: str) -> str:
    if type(state) is not str:
        raise TypeError("state must be a string")
    if state not in EXPOSURE_STATES:
        raise ValueError("state is invalid")
    return EXPOSURE_STATES[state]


def _core_exposure_clock(previous: ExposureClock | None) -> StateClock | None:
    if previous is not None and not isinstance(previous, ExposureClock):
        raise TypeError("previous must be ExposureClock or None")
    if previous is None:
        return None
    return StateClock(
        _core_exposure_state(previous.state),
        previous.observed_ns,
        previous.naked_since_ns,
        previous.duration_exceeded,
    )


def advance_exposure_clock(
    previous: ExposureClock | None,
    *,
    state: str,
    observed_ns: int,
    max_naked_ns: int,
) -> ExposureClock:
    """Advance naked-exposure duration using authoritative observations only."""
    result = advance_state_clock(
        _core_exposure_clock(previous),
        state=_core_exposure_state(state),
        observed_ns=observed_ns,
        max_active_ns=max_naked_ns,
    )
    return ExposureClock(
        CORE_EXPOSURE_STATES[result.state],
        result.observed_ns,
        result.active_since_ns,
        result.duration_exceeded,
    )


def net_delta(
    positions: Sequence[LegPosition],
    *,
    symbol: str,
    now_ns: int,
    max_age_ns: int,
) -> Decimal | None:
    """Return exact signed delta, or None when either venue position is unknowable."""
    now = _positive_int(now_ns, "now_ns")
    max_age = _positive_int(max_age_ns, "max_age_ns")
    legs = tuple(positions)
    if len(legs) != len(VENUES):
        raise ValueError("position venue set must contain exactly two venues")
    if {leg.venue for leg in legs} != VENUES:
        raise ValueError("position venue set is invalid")
    for leg in legs:
        _validate_leg(leg, symbol)
    if not all(
        surface_is_authoritative(leg.evidence, now_ns=now, max_age_ns=max_age) for leg in legs
    ):
        return None
    return sum((leg.signed_quantity for leg in legs), start=Decimal(0))
