"""Gate 4 stop detectors, distinct from Gate 3 risk-increasing admission.

Gate 3 asks whether new risk may be opened; Gate 4 independently asks whether
the whole execution system must stop. Their current seven-day wallet thresholds
therefore remain separate policies even though their durations are equal.

Nonce anomalies here are limited to the signer allocation stream. Order-request
binding anomalies require a separate Goal decision rather than implicit expansion.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from execution.nonce import replay_freeze_reason, replay_signer_nonce_conflict
from execution.wallet import AgentWalletRegistration
from reconciliation.exposure import ExposureClock
from reconciliation.fx import Notional

KILL_SWITCH_KEY_EXPIRY_LEAD_NS = 7 * 86_400 * 1_000_000_000


@dataclass(frozen=True, slots=True)
class KillSwitchDecision:
    action: Literal["continue", "flatten_and_stop", "cancel_only_freeze"]

    def __post_init__(self) -> None:
        if self.action not in {"continue", "flatten_and_stop", "cancel_only_freeze"}:
            raise ValueError("invalid kill switch action")


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
    *, triggered: bool, orders_known: bool, positions_known: bool,
    reconciliation_consistency: bool | None,
    reconciliation_streak_triggered: bool,
) -> KillSwitchDecision:
    """Choose a stop action without flattening through unknown venue state."""
    for name, value in (
        ("triggered", triggered),
        ("orders_known", orders_known),
        ("positions_known", positions_known),
        ("reconciliation_streak_triggered", reconciliation_streak_triggered),
    ):
        if type(value) is not bool:
            raise TypeError(f"{name} must be a boolean")
    if reconciliation_consistency is not None and type(reconciliation_consistency) is not bool:
        raise TypeError("reconciliation_consistency must be a boolean or None")
    unknown = not orders_known or not positions_known or reconciliation_consistency is None
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
