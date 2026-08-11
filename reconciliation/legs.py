"""Fail-closed completion states for one execution leg."""

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

COMPLETIONS = frozenset({"none", "partial", "complete", "overfilled", "unknown"})


@dataclass(frozen=True, slots=True)
class LegOutcome:
    venue: str
    completion: str


@dataclass(frozen=True, slots=True)
class PairState:
    state: str
    unresolved: tuple[tuple[str, str], ...]


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _validate_quantities(intended_quantity: Decimal, filled_quantity: Decimal | None) -> None:
    if type(intended_quantity) is not Decimal:
        raise TypeError("intended_quantity must be Decimal")
    if filled_quantity is not None and type(filled_quantity) is not Decimal:
        raise TypeError("filled_quantity must be Decimal or None")
    if not intended_quantity.is_finite():
        raise ValueError("intended_quantity must be finite")
    if filled_quantity is not None and not filled_quantity.is_finite():
        raise ValueError("filled_quantity must be finite")
    if intended_quantity <= 0:
        raise ValueError("intended_quantity must be positive")
    if filled_quantity is not None and filled_quantity < 0:
        raise ValueError("filled_quantity must be non-negative")


def leg_completion(
    *,
    intended_quantity: Decimal,
    filled_quantity: Decimal | None,
    evidence: SurfaceEvidence,
    now_ns: int,
    max_age_ns: int,
) -> str:
    """Classify one leg without treating incomplete evidence as completion."""
    _validate_quantities(intended_quantity, filled_quantity)
    now = _positive_int(now_ns, "now_ns")
    max_age = _positive_int(max_age_ns, "max_age_ns")
    if filled_quantity is None:
        return "unknown"
    observed_ns = evidence.observed_ns if isinstance(evidence, SurfaceEvidence) else 0
    validate_surface_evidence(evidence, now_ns=observed_ns)
    if not surface_is_authoritative(evidence, now_ns=now, max_age_ns=max_age):
        return "unknown"
    if filled_quantity == 0:
        return "none"
    if filled_quantity < intended_quantity:
        return "partial"
    if filled_quantity == intended_quantity:
        return "complete"
    return "overfilled"


def _validated_outcomes(legs: Sequence[LegOutcome]) -> tuple[LegOutcome, ...]:
    outcomes = tuple(legs)
    if len(outcomes) != len(VENUES):
        raise ValueError("pair venue set must contain exactly two venues")
    for outcome in outcomes:
        if not isinstance(outcome, LegOutcome):
            raise TypeError("pair member must be LegOutcome")
        if type(outcome.venue) is not str:
            raise TypeError("venue must be a string")
        if type(outcome.completion) is not str:
            raise TypeError("completion must be a string")
        if outcome.completion not in COMPLETIONS:
            raise ValueError("completion is invalid")
    if {outcome.venue for outcome in outcomes} != VENUES:
        raise ValueError("pair venue set is invalid")
    return outcomes


def pair_state(legs: Sequence[LegOutcome]) -> PairState:
    """Combine exactly two venue leg outcomes without discarding obligations."""
    outcomes = _validated_outcomes(legs)
    unresolved = tuple(
        sorted(
            (outcome.venue, outcome.completion)
            for outcome in outcomes
            if outcome.completion != "complete"
        )
    )
    completions = {outcome.completion for outcome in outcomes}
    if "unknown" in completions:
        state = "unknown"
    elif "overfilled" in completions:
        state = "overfilled"
    elif completions == {"none"}:
        state = "unfilled"
    elif unresolved:
        state = "imbalanced"
    else:
        state = "balanced"
    return PairState(state, unresolved)


def obligation_state(pair: PairState) -> str:
    """Return whether both venue completion obligations are confirmed settled."""
    if not isinstance(pair, PairState):
        raise TypeError("pair must be PairState")
    return "outstanding" if pair.unresolved else "settled"


def advance_obligation_clock(
    previous: StateClock | None,
    *,
    pair: PairState,
    observed_ns: int,
    max_outstanding_ns: int,
) -> StateClock:
    """Advance pair-level unresolved obligation duration."""
    state = "active" if obligation_state(pair) == "outstanding" else "inactive"
    return advance_state_clock(
        previous,
        state=state,
        observed_ns=observed_ns,
        max_active_ns=max_outstanding_ns,
    )
