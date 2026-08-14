import inspect

import pytest

from data.coverage import (
    COMPLETION_WINDOW_HOURS,
    GATE1_WINDOW_HOURS,
    MAX_EXPLAINED_GAP_NS,
    MAX_EXPLAINED_GAP_PERCENT,
    MIN_LATENCY_USABLE_HOUR_PERCENT,
    UTC_DAY_NS,
    UTC_HOUR_NS,
    eligible_utc_hours,
    window_coverage_ok,
)

HOUR_NS = 3_600_000_000_000


def test_eligible_utc_window_contract_is_frozen_and_keyword_only() -> None:
    assert UTC_HOUR_NS == HOUR_NS
    assert UTC_DAY_NS == 24 * HOUR_NS
    assert GATE1_WINDOW_HOURS == 168
    assert COMPLETION_WINDOW_HOURS == 720
    parameters = inspect.signature(eligible_utc_hours).parameters
    assert list(parameters) == ["start_ns", "end_ns", "window_kind"]
    assert all(item.kind is inspect.Parameter.KEYWORD_ONLY for item in parameters.values())


def test_gate1_accepts_any_utc_hour_and_counts_half_open_hours() -> None:
    start_ns = HOUR_NS
    assert eligible_utc_hours(
        start_ns=start_ns,
        end_ns=start_ns + 168 * HOUR_NS,
        window_kind="gate1_7d",
    ) == 168


def test_completion_accepts_exactly_30_utc_calendar_days() -> None:
    assert eligible_utc_hours(
        start_ns=0,
        end_ns=720 * HOUR_NS,
        window_kind="completion_30d",
    ) == 720


@pytest.mark.parametrize(
    ("start_ns", "end_ns"),
    [
        (1, 168 * HOUR_NS),
        (0, 168 * HOUR_NS + 1),
    ],
)
def test_utc_window_endpoints_require_exact_hour_alignment(
    start_ns: int,
    end_ns: int,
) -> None:
    with pytest.raises(ValueError, match="hour alignment"):
        eligible_utc_hours(start_ns=start_ns, end_ns=end_ns, window_kind="gate1_7d")


@pytest.mark.parametrize(
    ("window_kind", "hours"),
    [
        ("gate1_7d", 167),
        ("gate1_7d", 169),
        ("completion_30d", 719),
        ("completion_30d", 721),
    ],
)
def test_utc_window_duration_is_exact(window_kind: str, hours: int) -> None:
    with pytest.raises(ValueError, match="duration"):
        eligible_utc_hours(start_ns=0, end_ns=hours * HOUR_NS, window_kind=window_kind)


def test_completion_window_must_start_at_utc_midnight() -> None:
    start_ns = HOUR_NS
    with pytest.raises(ValueError, match="day aligned"):
        eligible_utc_hours(
            start_ns=start_ns,
            end_ns=start_ns + 720 * HOUR_NS,
            window_kind="completion_30d",
        )


@pytest.mark.parametrize(
    ("start_ns", "end_ns", "window_kind"),
    [
        (0, 0, "gate1_7d"),
        (HOUR_NS, 0, "gate1_7d"),
        (-HOUR_NS, 167 * HOUR_NS, "gate1_7d"),
        (False, 168 * HOUR_NS, "gate1_7d"),
        (0.0, 168 * HOUR_NS, "gate1_7d"),
        (0, True, "gate1_7d"),
        (0, float(168 * HOUR_NS), "gate1_7d"),
        (0, 168 * HOUR_NS, "other"),
        (0, 168 * HOUR_NS, False),
    ],
)
def test_utc_window_inputs_are_closed_exact_and_ordered(
    start_ns: object,
    end_ns: object,
    window_kind: object,
) -> None:
    with pytest.raises(ValueError):
        eligible_utc_hours(start_ns=start_ns, end_ns=end_ns, window_kind=window_kind)


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
