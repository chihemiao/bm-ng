from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from execution.wallet import AgentWalletRegistration
from reconciliation import exposure, kill_switch, state


@dataclass(frozen=True, slots=True, kw_only=True)
class KillSwitchSnapshotInputs:
    registration: AgentWalletRegistration
    nonce_events: Sequence[Mapping[str, object]]
    previous_streak: kill_switch.ReconciliationStreak | None
    streak_threshold: int
    venues: Mapping[str, state.VenueEvidence]
    expectations: Mapping[str, state.VenueExpectation]
    delta: Decimal | None
    reconciliation_observed_ns: int
    now_ns: int
    max_age_ns: int


@dataclass(frozen=True, slots=True)
class KillSwitchSnapshot:
    decision: kill_switch.KillSwitchDecision
    reconciliation_streak: kill_switch.ReconciliationStreak | None
    reconciliation_consistency: bool | None


def build_kill_switch_snapshot(inputs: KillSwitchSnapshotInputs) -> KillSwitchSnapshot:
    if not isinstance(inputs, KillSwitchSnapshotInputs):
        raise TypeError("inputs must be KillSwitchSnapshotInputs")
    cycle, now = inputs.reconciliation_observed_ns, inputs.now_ns
    invalid_type = type(cycle) is not int or type(now) is not int
    if invalid_type or not 0 < cycle <= now:
        raise (TypeError if invalid_type else ValueError)("snapshot times are invalid")
    consistency = state.classify_reconciliation_consistency(
        now_ns=now, max_age_ns=inputs.max_age_ns,
        venues=inputs.venues, expectations=inputs.expectations)
    if max(
        getattr(evidence, name).observed_ns
        for evidence in inputs.venues.values() for name in state.SURFACES) > cycle:
        raise ValueError("reconciliation cycle predates surface evidence")
    previous, threshold = inputs.previous_streak, inputs.streak_threshold
    if previous is not None and not isinstance(previous, kill_switch.ReconciliationStreak):
        raise TypeError("previous_streak must be ReconciliationStreak or None")
    if previous is not None and previous.last_observed_ns > cycle:
        raise ValueError("reconciliation cycle moved backward")
    if (invalid_type := type(threshold) is not int) or threshold <= 0:
        raise (TypeError if invalid_type else ValueError)("streak_threshold must be positive")
    streak = previous if consistency is None else kill_switch.advance_reconciliation_streak(
        previous, consistent=consistency, observed_ns=cycle)
    reached = False if streak is None else kill_switch.reconciliation_streak_triggered(
        streak, threshold=threshold)
    orders_known = all(state.surface_is_authoritative(
        evidence.orders, now_ns=now, max_age_ns=inputs.max_age_ns)
        for evidence in inputs.venues.values())
    positions_known = exposure.delta_state(inputs.delta, tolerance=Decimal(0)) != "unknown"
    triggered = kill_switch.key_and_nonce_triggered(
        inputs.registration, inputs.nonce_events, now_ns=now)
    decision = kill_switch.decide_kill_switch(
        triggered=triggered, orders_known=orders_known,
        positions_known=positions_known, naked_notional_known=True,
        reconciliation_consistency=consistency,
        reconciliation_streak_triggered=reached)
    return KillSwitchSnapshot(decision, streak, consistency)
