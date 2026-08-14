from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from data.collector import CollectorLivenessSnapshot
from execution.orders import FlattenIntentPlan
from execution.wallet import AgentWalletRegistration
from reconciliation import exposure, fx, kill_switch, legs, state


@dataclass(frozen=True, slots=True, kw_only=True)
class KillSwitchSnapshotInputs:
    registration: AgentWalletRegistration
    nonce_events: Sequence[Mapping[str, object]]
    previous_streak: kill_switch.ReconciliationStreak | None
    streak_threshold: int
    venues: Mapping[str, state.VenueEvidence]
    expectations: Mapping[str, state.VenueExpectation]
    delta: Decimal | None
    previous_exposure: exposure.ExposureClock | None
    delta_tolerance: Decimal
    max_naked_ns: int
    naked_notional: fx.Notional | None
    max_naked_notional: fx.Notional
    fx_rate: fx.FxRate | None
    fx_max_age_ns: int
    max_abs_fx_deviation: Decimal
    liveness: CollectorLivenessSnapshot
    max_data_gap_ns: int
    reconciliation_observed_ns: int
    now_ns: int
    max_age_ns: int


@dataclass(frozen=True, slots=True)
class KillSwitchSnapshot:
    decision: kill_switch.KillSwitchDecision
    reconciliation_streak: kill_switch.ReconciliationStreak | None
    reconciliation_consistency: bool | None
    exposure: exposure.ExposureClock


def build_kill_switch_snapshot(inputs: KillSwitchSnapshotInputs) -> KillSwitchSnapshot:
    if not isinstance(inputs, KillSwitchSnapshotInputs):
        raise TypeError("inputs must be KillSwitchSnapshotInputs")
    cycle, now = inputs.reconciliation_observed_ns, inputs.now_ns
    invalid_type = type(cycle) is not int or type(now) is not int
    if invalid_type or not 0 < cycle <= now:
        raise (TypeError if invalid_type else ValueError)("snapshot times are invalid")
    exposure_state = exposure.advance_exposure_clock(
        inputs.previous_exposure,
        state=exposure.delta_state(inputs.delta, tolerance=inputs.delta_tolerance),
        observed_ns=cycle, max_naked_ns=inputs.max_naked_ns)
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
    positions_known = exposure_state.state != "unknown"
    key_triggered = kill_switch.key_and_nonce_triggered(
        inputs.registration, inputs.nonce_events, now_ns=now)
    exposure_triggered = kill_switch.exposure_kill_trigger(
        exposure_state, inputs.naked_notional,
        max_naked_notional=inputs.max_naked_notional)
    stablecoin_known, stablecoin_triggered = kill_switch._stablecoin_evidence(
        inputs.fx_rate, now_ns=now, max_age_ns=inputs.fx_max_age_ns,
        max_abs_deviation=inputs.max_abs_fx_deviation)
    data_known, data_triggered = kill_switch.data_liveness_evidence(
        inputs.liveness, now_ns=now, max_gap_ns=inputs.max_data_gap_ns)
    triggered = key_triggered or exposure_triggered or stablecoin_triggered or data_triggered
    decision = kill_switch.decide_kill_switch(
        triggered=triggered, known_evidence=kill_switch.KnownEvidence(
            orders=orders_known,
            positions=positions_known,
            naked_notional=inputs.naked_notional is not None,
            stablecoin=stablecoin_known,
            data_liveness=data_known,
        ),
        reconciliation_consistency=consistency,
        reconciliation_streak_triggered=reached)
    return KillSwitchSnapshot(decision, streak, consistency, exposure_state)


def build_kill_switch_flatten_plan(
    decision: kill_switch.KillSwitchDecision,
    hyperliquid_position: object,
    bybit_position: object,
    *,
    strategy_id: str,
    strategy_version: str,
    signal_ns: int,
    now_ns: int,
    max_position_age_ns: int,
) -> FlattenIntentPlan | None:
    """Plan trusted flatten intents only for the explicit flatten action."""
    if not isinstance(decision, kill_switch.KillSwitchDecision):
        raise TypeError("decision must be a KillSwitchDecision")
    if decision.action != "flatten_and_stop":
        return None
    return legs.build_flatten_intent_plan(
        hyperliquid_position,
        bybit_position,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        signal_ns=signal_ns,
        now_ns=now_ns,
        max_position_age_ns=max_position_age_ns,
    )
