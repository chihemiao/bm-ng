"""Fail-closed completion states for one execution leg."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from data.shard import EventReplay
from execution.order_serde import rehydrate_order_request
from execution.orders import (
    ReconciliationEvidence,
    T0APairIntents,
    t0a_pair_intents_match,
)
from reconciliation.bybit_surface import (
    BybitFilledQuantity,
    build_intent_bybit_filled_quantity,
)
from reconciliation.clock import StateClock, advance_state_clock
from reconciliation.hl_fills import HLFilledQuantity, build_replayed_hl_filled_quantity
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


def build_order_reconciliation_evidence(
    replay: EventReplay, *, client_order_id: str, venue: str,
) -> ReconciliationEvidence:
    """Select one unambiguous latest order observation from durable replay."""
    if type(replay) is not EventReplay:
        raise TypeError("replay must be an EventReplay")
    if not isinstance(client_order_id, str):
        raise TypeError("client_order_id must be a string")
    if not client_order_id:
        raise ValueError("client_order_id must not be empty")
    if not isinstance(venue, str):
        raise TypeError("venue must be a string")
    if venue not in VENUES:
        raise ValueError("venue is invalid")
    unknown = ReconciliationEvidence("unknown", None, None, None)
    if replay.freeze_reasons:
        return unknown
    matches = [
        event
        for event in replay.events
        if event["payload_schema"] == "order_observation"
        and event["venue"] == venue
        and event["client_order_id"] == client_order_id
    ]
    if not matches:
        return unknown
    latest_ns = max(event["payload"]["observed_ns"] for event in matches)
    statuses = {
        event["payload"]["status"]
        for event in matches
        if event["payload"]["observed_ns"] == latest_ns
    }
    if len(statuses) != 1:
        return unknown
    status = statuses.pop()
    if status == "unknown":
        return unknown
    return ReconciliationEvidence(status, latest_ns, None, None)


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


def build_fill_pair_state(
    pair: T0APairIntents,
    hl_result: HLFilledQuantity | None,
    bybit_result: BybitFilledQuantity,
    *,
    now_ns: int,
    max_age_ns: int,
) -> PairState:
    """Bind fills to one pair; missing HL bindings are unknown, never zero."""
    if not isinstance(pair, T0APairIntents):
        raise TypeError("pair must be T0APairIntents")
    if hl_result is not None and not isinstance(hl_result, HLFilledQuantity):
        raise TypeError("hl_result must be HLFilledQuantity")
    if not isinstance(bybit_result, BybitFilledQuantity):
        raise TypeError("bybit_result must be BybitFilledQuantity")
    if not t0a_pair_intents_match(pair):
        raise ValueError("pair intents do not match T0A topology")
    if hl_result is not None and hl_result.client_order_id != pair.hyperliquid.client_order_id:
        raise ValueError("hl_result does not match hyperliquid intent")
    if bybit_result.client_order_id != pair.bybit.client_order_id:
        raise ValueError("bybit_result does not match bybit intent")

    outcomes = []
    for venue, intent, result in (
        ("hyperliquid", pair.hyperliquid, hl_result),
        ("bybit", pair.bybit, bybit_result),
    ):
        completion = "unknown" if result is None else leg_completion(
            intended_quantity=intent.quantity,
            filled_quantity=result.quantity,
            evidence=result.evidence,
            now_ns=now_ns,
            max_age_ns=max_age_ns,
        )
        outcomes.append(LegOutcome(venue, completion))
    return pair_state(outcomes)


def build_replayed_fill_pair_state(
    pair: T0APairIntents,
    *,
    replay: EventReplay,
    hyperliquid_pages: Sequence[list[object]],
    bybit_pages: Sequence[Mapping[str, object]],
    since_ms: int,
    skew_allowance_ms: int,
    observed_ns: int,
    page_complete: bool,
    truncated: bool,
    now_ns: int,
    max_age_ns: int,
) -> PairState:
    """Assemble one submitted pair from durable requests and venue fill surfaces."""
    if not isinstance(pair, T0APairIntents):
        raise TypeError("pair must be T0APairIntents")
    if not t0a_pair_intents_match(pair):
        raise ValueError("pair intents do not match T0A topology")
    if type(replay) is not EventReplay:
        raise TypeError("replay must be an EventReplay")
    requests = [
        rehydrate_order_request(event)[0]
        for event in replay.events
        if event["payload_schema"] == "order_request"
    ]
    if pair.hyperliquid not in requests or pair.bybit not in requests:
        raise ValueError("pair durable requests are incomplete")
    hl_result = build_replayed_hl_filled_quantity(
        replay, hyperliquid_pages, intent=pair.hyperliquid, since_ms=since_ms,
        skew_allowance_ms=skew_allowance_ms, observed_ns=observed_ns,
        page_complete=page_complete, truncated=truncated,
    )
    bybit_result = build_intent_bybit_filled_quantity(
        bybit_pages, intent=pair.bybit, since_ms=since_ms,
        skew_allowance_ms=skew_allowance_ms, observed_ns=observed_ns,
    )
    return build_fill_pair_state(
        pair, hl_result, bybit_result, now_ns=now_ns, max_age_ns=max_age_ns
    )


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
