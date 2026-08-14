"""Gate 4 stop detectors, distinct from Gate 3 risk-increasing admission.

Gate 3 asks whether new risk may be opened; Gate 4 independently asks whether
the whole execution system must stop. Their current seven-day wallet thresholds
therefore remain separate policies even though their durations are equal.

Nonce anomalies here are limited to the signer allocation stream. Order-request
binding anomalies require a separate Goal decision rather than implicit expansion.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from data.collector import CollectorLivenessSnapshot
from execution.nonce import replay_freeze_reason, replay_signer_nonce_conflict
from execution.wallet import AgentWalletRegistration
from reconciliation.exposure import ExposureClock
from reconciliation.fx import FxRate, Notional, convert_usdt_to_usdc

KILL_SWITCH_KEY_EXPIRY_LEAD_NS = 7 * 86_400 * 1_000_000_000


@dataclass(frozen=True, slots=True)
class KillSwitchDecision:
    action: Literal["continue", "flatten_and_stop", "cancel_only_freeze"]

    def __post_init__(self) -> None:
        if self.action not in {"continue", "flatten_and_stop", "cancel_only_freeze"}:
            raise ValueError("invalid kill switch action")


@dataclass(frozen=True, slots=True, kw_only=True)
class KnownEvidence:
    orders: bool
    positions: bool
    naked_notional: bool
    stablecoin: bool
    data_liveness: bool

    def __post_init__(self) -> None:
        for name in (
            "orders", "positions", "naked_notional", "stablecoin", "data_liveness",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconciliationStreak:
    count: int
    last_observed_ns: int

    def __post_init__(self) -> None:
        if type(self.count) is not int:
            raise TypeError("count must be an integer")
        if self.count < 0:
            raise ValueError("count must be non-negative")
        if type(self.last_observed_ns) is not int:
            raise TypeError("last_observed_ns must be an integer")
        if self.last_observed_ns <= 0:
            raise ValueError("last_observed_ns must be positive")


def advance_reconciliation_streak(
    previous: ReconciliationStreak | None,
    *,
    consistent: bool,
    observed_ns: int,
) -> ReconciliationStreak:
    """Count distinct consecutive inconsistent reconciliation observations."""
    if previous is not None and not isinstance(previous, ReconciliationStreak):
        raise TypeError("previous must be ReconciliationStreak or None")
    if type(consistent) is not bool:
        raise TypeError("consistent must be a boolean")
    if type(observed_ns) is not int:
        raise TypeError("observed_ns must be an integer")
    if observed_ns <= 0:
        raise ValueError("observed_ns must be positive")
    if previous is not None:
        if observed_ns < previous.last_observed_ns:
            raise ValueError("observed_ns cannot move backward")
        if observed_ns == previous.last_observed_ns:
            if consistent != (previous.count == 0):
                raise ValueError("different consistency at same observed_ns")
            return previous
    count = 0 if consistent else (previous.count if previous is not None else 0) + 1
    return ReconciliationStreak(count=count, last_observed_ns=observed_ns)


def reconciliation_streak_triggered(
    streak: ReconciliationStreak, *, threshold: int,
) -> bool:
    """Return whether consecutive inconsistency has reached K observations."""
    if not isinstance(streak, ReconciliationStreak):
        raise TypeError("streak must be a ReconciliationStreak")
    if type(threshold) is not int:
        raise TypeError("threshold must be an integer")
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    return streak.count >= threshold


def decide_kill_switch(
    *, triggered: bool, known_evidence: KnownEvidence,
    reconciliation_consistency: bool | None,
    reconciliation_streak_triggered: bool,
) -> KillSwitchDecision:
    """Choose a stop action without flattening through unknown venue state."""
    if not isinstance(known_evidence, KnownEvidence):
        raise TypeError("known_evidence must be a KnownEvidence")
    for name, value in (
        ("triggered", triggered),
        ("reconciliation_streak_triggered", reconciliation_streak_triggered),
    ):
        if type(value) is not bool:
            raise TypeError(f"{name} must be a boolean")
    if reconciliation_consistency is not None and type(reconciliation_consistency) is not bool:
        raise TypeError("reconciliation_consistency must be a boolean or None")
    unknown = not all((
        known_evidence.orders,
        known_evidence.positions,
        known_evidence.naked_notional,
        known_evidence.stablecoin,
        known_evidence.data_liveness,
    )) or reconciliation_consistency is None
    if unknown or reconciliation_streak_triggered:
        return KillSwitchDecision("cancel_only_freeze")
    if triggered and not reconciliation_consistency:
        return KillSwitchDecision("cancel_only_freeze")
    action = "flatten_and_stop" if triggered else "continue"
    return KillSwitchDecision(action)


def exposure_kill_trigger(
    exposure: ExposureClock,
    naked_notional: Notional | None,
    *,
    max_naked_notional: Notional,
) -> bool:
    """Trigger when cross-venue exposure is unknown, overdue, or over limit."""
    if not isinstance(exposure, ExposureClock):
        raise TypeError("exposure must be an ExposureClock")
    if naked_notional is not None and not isinstance(naked_notional, Notional):
        raise TypeError("naked_notional must be Notional or None")
    if not isinstance(max_naked_notional, Notional):
        raise TypeError("max_naked_notional must be Notional")
    if naked_notional is not None and naked_notional.quote != max_naked_notional.quote:
        raise ValueError("notional quote mismatch")
    return (
        exposure.state == "unknown"
        or naked_notional is None
        or exposure.duration_exceeded is True
        or naked_notional.amount > max_naked_notional.amount
    )


def _fx_rate_fresh(rate: FxRate | None, *, now_ns: int, max_age_ns: int) -> bool:
    return convert_usdt_to_usdc(
        Decimal(1), rate=rate, now_ns=now_ns, max_age_ns=max_age_ns,
    ) is not None


def _stablecoin_evidence(
    rate: FxRate | None, *, now_ns: int, max_age_ns: int,
    max_abs_deviation: Decimal,
) -> tuple[bool, bool]:
    if type(max_abs_deviation) is not Decimal:
        raise TypeError("max_abs_deviation must be Decimal")
    if not max_abs_deviation.is_finite() or max_abs_deviation < 0:
        raise ValueError("max_abs_deviation must be finite and nonnegative")
    known = _fx_rate_fresh(rate, now_ns=now_ns, max_age_ns=max_age_ns)
    triggered = not known or abs(rate.rate - Decimal(1)) > max_abs_deviation
    return known, triggered


def stablecoin_spread_known(
    rate: FxRate | None, *, now_ns: int, max_age_ns: int,
) -> bool:
    """Return whether the direct USDT/USDC rate is valid, current evidence."""
    return _fx_rate_fresh(rate, now_ns=now_ns, max_age_ns=max_age_ns)


def stablecoin_spread_trigger(
    rate: FxRate | None,
    *,
    now_ns: int,
    max_age_ns: int,
    max_abs_deviation: Decimal,
) -> bool:
    """Trigger on missing evidence or deviation strictly beyond the limit."""
    return _stablecoin_evidence(
        rate, now_ns=now_ns, max_age_ns=max_age_ns,
        max_abs_deviation=max_abs_deviation,
    )[1]


def data_liveness_evidence(
    snapshot: CollectorLivenessSnapshot, *, now_ns: int, max_gap_ns: int,
) -> tuple[bool, bool]:
    """Classify hard data evidence and local monotonic self-consistency."""
    if not isinstance(snapshot, CollectorLivenessSnapshot):
        raise TypeError("snapshot must be a CollectorLivenessSnapshot")
    for name, value in (("now_ns", now_ns), ("max_gap_ns", max_gap_ns)):
        if type(value) is not int:
            raise TypeError(f"{name} must be an integer")
    if now_ns <= 0:
        raise ValueError("now_ns must be positive")
    if max_gap_ns < 0:
        raise ValueError("max_gap_ns must be non-negative")
    timestamps = (
        snapshot.hl_last_verified_mono_ns,
        snapshot.bybit_last_verified_mono_ns,
    )
    known = snapshot.file_integrity_ok and all(value is not None for value in timestamps)
    if not known:
        return False, True
    triggered = any(value > now_ns or now_ns - value > max_gap_ns for value in timestamps)
    return True, triggered


def key_expiry_triggered(
    registration: AgentWalletRegistration, *, now_ns: int,
) -> bool:
    """Return whether the agent key has strictly less than seven days left."""
    if not isinstance(registration, AgentWalletRegistration):
        raise TypeError("registration must be an AgentWalletRegistration")
    if type(now_ns) is not int:
        raise TypeError("now_ns must be an integer")
    if now_ns <= 0:
        raise ValueError("now_ns must be positive")
    return registration.expires_ns - now_ns < KILL_SWITCH_KEY_EXPIRY_LEAD_NS


def nonce_anomaly_triggered(
    events: Sequence[Mapping[str, object]], *, wallet_fingerprint: str,
) -> bool:
    """Return whether one signer allocation stream contains a nonce anomaly."""
    rows = tuple(events)
    freeze = replay_freeze_reason(rows, wallet_fingerprint)
    conflict = replay_signer_nonce_conflict(rows, wallet_fingerprint)
    return freeze is not None or conflict is not None


def key_and_nonce_triggered(
    registration: AgentWalletRegistration,
    nonce_events: Sequence[Mapping[str, object]],
    *,
    now_ns: int,
) -> bool:
    """Combine current key lifetime and the matching durable nonce stream."""
    if not isinstance(nonce_events, Sequence) or isinstance(nonce_events, (str, bytes)):
        raise TypeError("nonce_events must be a sequence")
    key = key_expiry_triggered(registration, now_ns=now_ns)
    nonce = nonce_anomaly_triggered(
        nonce_events, wallet_fingerprint=registration.wallet_fingerprint,
    )
    return key or nonce
