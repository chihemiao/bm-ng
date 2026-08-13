import inspect
from dataclasses import FrozenInstanceError, fields

import pytest

from reconciliation.kill_switch import (
    ReconciliationStreak,
    advance_reconciliation_streak,
    reconciliation_streak_triggered,
)


def _advance(previous, consistent, observed_ns):
    return advance_reconciliation_streak(
        previous, consistent=consistent, observed_ns=observed_ns,
    )


def test_reconciliation_streak_counts_consecutive_mismatches_and_resets() -> None:
    first = _advance(None, False, 100)
    second = _advance(first, False, 101)
    third = _advance(second, False, 102)
    assert (first.count, second.count, third.count) == (1, 2, 3)
    reset = _advance(third, True, 103)
    assert reset == ReconciliationStreak(count=0, last_observed_ns=103)
    assert _advance(reset, False, 104).count == 1
    assert _advance(None, True, 100).count == 0


@pytest.mark.parametrize("consistent", [False, True])
def test_reconciliation_streak_same_observation_is_idempotent(consistent) -> None:
    current = _advance(None, consistent, 100)
    assert _advance(current, consistent, 100) is current


def test_reconciliation_streak_rejects_conflicting_or_backward_observations() -> None:
    current = _advance(None, False, 100)
    with pytest.raises(ValueError, match="same observed_ns"):
        _advance(current, True, 100)
    with pytest.raises(ValueError, match="backward"):
        _advance(current, False, 99)


@pytest.mark.parametrize(
    ("previous", "consistent", "observed_ns", "error"),
    [
        (object(), False, 100, TypeError),
        (None, 0, 100, TypeError),
        (None, False, True, TypeError),
        (None, False, 0, ValueError),
    ],
)
def test_reconciliation_streak_requires_valid_inputs(
    previous, consistent, observed_ns, error,
) -> None:
    with pytest.raises(error):
        _advance(previous, consistent, observed_ns)


@pytest.mark.parametrize(
    ("count", "threshold", "triggered"), [(2, 3, False), (3, 3, True), (4, 3, True)],
)
def test_reconciliation_streak_triggers_at_threshold(count, threshold, triggered) -> None:
    streak = ReconciliationStreak(count=count, last_observed_ns=100)
    assert reconciliation_streak_triggered(streak, threshold=threshold) is triggered


@pytest.mark.parametrize(
    ("streak", "threshold", "error"),
    [
        (object(), 3, TypeError),
        (ReconciliationStreak(count=0, last_observed_ns=100), True, TypeError),
        (ReconciliationStreak(count=0, last_observed_ns=100), 0, ValueError),
    ],
)
def test_reconciliation_streak_trigger_requires_valid_inputs(streak, threshold, error) -> None:
    with pytest.raises(error):
        reconciliation_streak_triggered(streak, threshold=threshold)


def test_reconciliation_streak_has_only_durable_observation_state() -> None:
    assert [field.name for field in fields(ReconciliationStreak)] == [
        "count", "last_observed_ns",
    ]
    streak = ReconciliationStreak(count=0, last_observed_ns=100)
    with pytest.raises(FrozenInstanceError):
        streak.count = 1
    assert ReconciliationStreak.__slots__ == ("count", "last_observed_ns")
    parameters = inspect.signature(ReconciliationStreak).parameters.values()
    assert all(value.kind is inspect.Parameter.KEYWORD_ONLY for value in parameters)
