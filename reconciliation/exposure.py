"""Fail-closed cross-venue exposure arithmetic."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from reconciliation.state import (
    VENUES,
    SurfaceEvidence,
    surface_is_authoritative,
    validate_surface_evidence,
)


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


def _validate_clock_inputs(
    previous: ExposureClock | None,
    state: str,
    observed_ns: int,
    max_naked_ns: int,
) -> int:
    if previous is not None and not isinstance(previous, ExposureClock):
        raise TypeError("previous must be ExposureClock or None")
    if type(state) is not str:
        raise TypeError("state must be a string")
    if state not in {"flat", "naked", "unknown"}:
        raise ValueError("state is invalid")
    observed = _positive_int(observed_ns, "observed_ns")
    if type(max_naked_ns) is not int:
        raise TypeError("max_naked_ns must be an integer")
    if max_naked_ns < 0:
        raise ValueError("max_naked_ns must be non-negative")
    if previous is not None:
        if observed < previous.observed_ns:
            raise ValueError("observed_ns cannot move backward")
        if observed == previous.observed_ns and state != previous.state:
            raise ValueError("different state at same observed_ns")
    return observed


def advance_exposure_clock(
    previous: ExposureClock | None,
    *,
    state: str,
    observed_ns: int,
    max_naked_ns: int,
) -> ExposureClock:
    """Advance naked-exposure duration using authoritative observations only."""
    observed = _validate_clock_inputs(previous, state, observed_ns, max_naked_ns)
    if state == "flat":
        return ExposureClock(state, observed, None, False)
    naked_since = previous.naked_since_ns if previous is not None else None
    if state == "naked" and naked_since is None:
        naked_since = observed
    if naked_since is None:
        return ExposureClock(state, observed, None, None)
    exceeded = observed - naked_since > max_naked_ns
    return ExposureClock(state, observed, naked_since, exceeded)


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
