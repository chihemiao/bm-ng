import inspect

import pytest

from data.coverage import (
    MAX_EXPLAINED_GAP_NS,
    MAX_EXPLAINED_GAP_PERCENT,
    MIN_LATENCY_USABLE_HOUR_PERCENT,
    window_coverage_ok,
)

HOUR_NS = 3_600_000_000_000


def _coverage(**changes) -> bool:
    values = {
        "window_duration_ns": 100 * HOUR_NS,
        "max_single_gap_ns": 2 * HOUR_NS,
        "explained_gap_total_ns": 2 * HOUR_NS,
        "unexplained_gap_present": False,
        "usable_hours": 95,
        "eligible_hours": 100,
    }
    values.update(changes)
    return window_coverage_ok(**values)


def test_gate1_window_thresholds_are_frozen_and_keyword_only() -> None:
    assert MAX_EXPLAINED_GAP_NS == 4 * HOUR_NS
    assert MAX_EXPLAINED_GAP_PERCENT == 2
    assert MIN_LATENCY_USABLE_HOUR_PERCENT == 95
    parameters = inspect.signature(window_coverage_ok).parameters
    assert list(parameters) == [
        "window_duration_ns", "max_single_gap_ns", "explained_gap_total_ns",
        "unexplained_gap_present", "usable_hours", "eligible_hours",
    ]
    assert all(item.kind is inspect.Parameter.KEYWORD_ONLY for item in parameters.values())


def test_each_frozen_threshold_is_inclusive_and_rejects_the_next_unit() -> None:
    thirty_days = 30 * 24 * HOUR_NS
    assert _coverage(
        window_duration_ns=thirty_days,
        max_single_gap_ns=MAX_EXPLAINED_GAP_NS,
        explained_gap_total_ns=MAX_EXPLAINED_GAP_NS,
    )
    assert not _coverage(
        window_duration_ns=thirty_days,
        max_single_gap_ns=MAX_EXPLAINED_GAP_NS + 1,
        explained_gap_total_ns=MAX_EXPLAINED_GAP_NS + 1,
    )
    assert _coverage()
    assert not _coverage(
        max_single_gap_ns=2 * HOUR_NS + 1,
        explained_gap_total_ns=2 * HOUR_NS + 1,
    )
    assert _coverage(usable_hours=95)
    assert not _coverage(usable_hours=94)


def test_unexplained_gap_dominates_only_after_structural_validation() -> None:
    assert _coverage(unexplained_gap_present=True) is False
    with pytest.raises(ValueError, match="window_duration_ns"):
        _coverage(window_duration_ns=0, unexplained_gap_present=True)


def test_zero_explained_gaps_are_a_valid_complete_window() -> None:
    assert _coverage(max_single_gap_ns=0, explained_gap_total_ns=0) is True


@pytest.mark.parametrize(
    "changes",
    [
        {"window_duration_ns": 0},
        {"window_duration_ns": -1},
        {"window_duration_ns": True},
        {"window_duration_ns": 1.0},
        {"max_single_gap_ns": -1},
        {"explained_gap_total_ns": -1},
        {"usable_hours": -1},
        {"eligible_hours": 0},
        {"usable_hours": False},
        {"eligible_hours": 100.0},
        {"unexplained_gap_present": 0},
        {"unexplained_gap_present": "false"},
    ],
)
def test_window_coverage_inputs_are_exact_and_bounded(changes: dict) -> None:
    with pytest.raises(ValueError):
        _coverage(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_single_gap_ns": 3, "explained_gap_total_ns": 2},
        {"explained_gap_total_ns": 100 * HOUR_NS + 1},
        {"max_single_gap_ns": 0, "explained_gap_total_ns": 1},
        {"usable_hours": 101},
    ],
)
def test_impossible_coverage_aggregates_are_rejected(changes: dict) -> None:
    with pytest.raises(ValueError):
        _coverage(**changes)
