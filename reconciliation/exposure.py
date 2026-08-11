"""Fail-closed cross-venue exposure arithmetic."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from reconciliation.state import VENUES, SurfaceEvidence, validate_surface_evidence


@dataclass(frozen=True, slots=True)
class LegPosition:
    venue: str
    symbol: str
    signed_quantity: Decimal
    evidence: SurfaceEvidence


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
    evidence = leg.evidence
    observed_ns = evidence.observed_ns if isinstance(evidence, SurfaceEvidence) else 0
    validate_surface_evidence(evidence, now_ns=observed_ns)


def _authoritative(evidence: SurfaceEvidence, now_ns: int, max_age_ns: int) -> bool:
    age_ns = now_ns - evidence.observed_ns
    return (
        evidence.page_complete
        and not evidence.truncated
        and evidence.unknown_count == 0
        and evidence.mismatch_count == 0
        and 0 <= age_ns <= max_age_ns
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
    if not all(_authoritative(leg.evidence, now, max_age) for leg in legs):
        return None
    return sum((leg.signed_quantity for leg in legs), start=Decimal(0))
