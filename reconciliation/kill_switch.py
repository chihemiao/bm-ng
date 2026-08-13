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

KILL_SWITCH_KEY_EXPIRY_LEAD_NS = 7 * 86_400 * 1_000_000_000


@dataclass(frozen=True, slots=True)
class KillSwitchDecision:
    action: Literal["continue", "flatten_and_stop", "cancel_only_freeze"]

    def __post_init__(self) -> None:
        if self.action not in {"continue", "flatten_and_stop", "cancel_only_freeze"}:
            raise ValueError("invalid kill switch action")


def decide_kill_switch(
    *, triggered: bool, orders_known: bool, positions_known: bool,
    reconciliation_consistent: bool,
) -> KillSwitchDecision:
    """Choose a stop action without flattening through unknown venue state."""
    for name, value in (
        ("triggered", triggered),
        ("orders_known", orders_known),
        ("positions_known", positions_known),
        ("reconciliation_consistent", reconciliation_consistent),
    ):
        if type(value) is not bool:
            raise TypeError(f"{name} must be a boolean")
    if not orders_known or not positions_known or not reconciliation_consistent:
        return KillSwitchDecision("cancel_only_freeze")
    action = "flatten_and_stop" if triggered else "continue"
    return KillSwitchDecision(action)


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
