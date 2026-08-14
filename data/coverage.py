"""Pure Gate 1 window arithmetic over already-classified coverage evidence."""

from typing import Literal

MAX_EXPLAINED_GAP_NS = 4 * 3_600 * 1_000_000_000
MAX_EXPLAINED_GAP_PERCENT = 2
MIN_LATENCY_USABLE_HOUR_PERCENT = 95
UTC_HOUR_NS = 3_600_000_000_000
UTC_DAY_NS = 24 * UTC_HOUR_NS
GATE1_WINDOW_HOURS = 168
COMPLETION_WINDOW_HOURS = 720


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _nonnegative_exact_int(value: object) -> bool:
    return type(value) is int and value >= 0


def eligible_utc_hours(
    *,
    start_ns: int,
    end_ns: int,
    window_kind: Literal["gate1_7d", "completion_30d"],
) -> int:
    """Validate one frozen half-open UTC coverage window and return its hours."""
    _require(type(start_ns) is int, "invalid start_ns type")
    _require(type(end_ns) is int, "invalid end_ns type")
    _require(start_ns >= 0 and end_ns > start_ns, "invalid UTC window range")
    _require(
        start_ns % UTC_HOUR_NS == 0 and end_ns % UTC_HOUR_NS == 0,
        "UTC hour alignment required",
    )
    _require(
        type(window_kind) is str
        and window_kind in {"gate1_7d", "completion_30d"},
        "invalid window_kind",
    )
    expected_hours = (
        GATE1_WINDOW_HOURS
        if window_kind == "gate1_7d"
        else COMPLETION_WINDOW_HOURS
    )
    actual_hours = (end_ns - start_ns) // UTC_HOUR_NS
    _require(actual_hours == expected_hours, "invalid window duration")
    _require(
        window_kind != "completion_30d" or start_ns % UTC_DAY_NS == 0,
        "completion window must be UTC day aligned",
    )
    return actual_hours


def window_coverage_ok(
    *,
    window_duration_ns: int,
    max_single_gap_ns: int,
    explained_gap_total_ns: int,
    unexplained_gap_present: bool,
    usable_hours: int,
    eligible_hours: int,
) -> bool:
    """Apply frozen thresholds without classifying gaps, hours, or recovery streaks."""
    numbers = {
        "window_duration_ns": window_duration_ns,
        "max_single_gap_ns": max_single_gap_ns,
        "explained_gap_total_ns": explained_gap_total_ns,
        "usable_hours": usable_hours,
        "eligible_hours": eligible_hours,
    }
    for name, value in numbers.items():
        _require(_nonnegative_exact_int(value), f"invalid {name}")
    _require(window_duration_ns > 0, "invalid window_duration_ns")
    _require(eligible_hours > 0, "invalid eligible_hours")
    _require(type(unexplained_gap_present) is bool, "invalid unexplained_gap_present")
    _require(max_single_gap_ns <= explained_gap_total_ns, "gap maximum exceeds total")
    _require(explained_gap_total_ns <= window_duration_ns, "gap total exceeds window")
    _require(bool(max_single_gap_ns) == bool(explained_gap_total_ns), "incomplete gap aggregate")
    _require(usable_hours <= eligible_hours, "usable hours exceed eligible hours")
    return all((
        not unexplained_gap_present,
        max_single_gap_ns <= MAX_EXPLAINED_GAP_NS,
        explained_gap_total_ns * 100 <= window_duration_ns * MAX_EXPLAINED_GAP_PERCENT,
        usable_hours * 100 >= eligible_hours * MIN_LATENCY_USABLE_HOUR_PERCENT,
    ))
