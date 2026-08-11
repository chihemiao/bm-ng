"""Shared fail-closed state duration clock."""

from dataclasses import dataclass

STATES = frozenset({"inactive", "active", "unknown"})


@dataclass(frozen=True, slots=True)
class StateClock:
    state: str
    observed_ns: int
    active_since_ns: int | None
    duration_exceeded: bool | None


def _integer_at_least(value: object, name: str, minimum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _validate_inputs(
    previous: StateClock | None, state: str, observed_ns: int, max_active_ns: int
) -> int:
    if previous is not None and not isinstance(previous, StateClock):
        raise TypeError("previous must be StateClock or None")
    if type(state) is not str:
        raise TypeError("state must be a string")
    if state not in STATES:
        raise ValueError("state is invalid")
    observed = _integer_at_least(observed_ns, "observed_ns", 1)
    _integer_at_least(max_active_ns, "max_active_ns", 0)
    if previous is not None:
        if observed < previous.observed_ns:
            raise ValueError("observed_ns cannot move backward")
        if observed == previous.observed_ns and state != previous.state:
            raise ValueError("different state at same observed_ns")
    return observed


def advance_state_clock(
    previous: StateClock | None,
    *,
    state: str,
    observed_ns: int,
    max_active_ns: int,
) -> StateClock:
    """Advance an observed active interval without guessing through unknown state."""
    observed = _validate_inputs(previous, state, observed_ns, max_active_ns)
    if state == "inactive":
        return StateClock(state, observed, None, False)
    active_since = previous.active_since_ns if previous is not None else None
    if state == "active" and active_since is None:
        active_since = observed
    if active_since is None:
        return StateClock(state, observed, None, None)
    exceeded = observed - active_since > max_active_ns
    return StateClock(state, observed, active_since, exceeded)
