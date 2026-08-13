"""Gate 4 stop detectors, distinct from Gate 3 risk-increasing admission.

Gate 3 asks whether new risk may be opened; Gate 4 independently asks whether
the whole execution system must stop. Their current seven-day wallet thresholds
therefore remain separate policies even though their durations are equal.
"""

from execution.wallet import AgentWalletRegistration

KILL_SWITCH_KEY_EXPIRY_LEAD_NS = 7 * 86_400 * 1_000_000_000


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
