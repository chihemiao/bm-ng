import inspect
from typing import Literal, get_args, get_type_hints

import pytest

from data.coverage import (
    COMPLETION_WINDOW_HOURS,
    EXPLAINED_FAILURE_REASONS,
    GATE1_WINDOW_HOURS,
    MAX_EXPLAINED_GAP_NS,
    MAX_EXPLAINED_GAP_PERCENT,
    MIN_LATENCY_USABLE_HOUR_PERCENT,
    UTC_DAY_NS,
    UTC_HOUR_NS,
    CoveragePoint,
    PairingResult,
    eligible_utc_hours,
    pair_explained_intervals,
    window_coverage_ok,
)

HOUR_NS = 3_600_000_000_000


def _point(
    observed_ns: int, kind: str = "hard_verified", *, venue: str = "hyperliquid",
    reason: str | None = None,
) -> CoveragePoint:
    reason = "venue_down" if kind == "explained_failure" and reason is None else reason
    return CoveragePoint(venue, observed_ns, kind, reason)


def _pair(points: list[CoveragePoint], *, start_ns: int = 0, end_ns: int = 100) -> PairingResult:
    return pair_explained_intervals(points, window_start_ns=start_ns, window_end_ns=end_ns)


def test_gap_pairing_contract_and_clean_results_are_frozen() -> None:
    assert CoveragePoint._fields == ("venue", "observed_ns", "kind", "reason")
    assert PairingResult._fields == ("explained_intervals", "unexplained_gap_present")
    reason_type = get_type_hints(CoveragePoint)["reason"]
    expected_reason_type = Literal[
        "application_pong_timeout",
        "subscription_ack_timeout",
        "transport_disconnected",
        "transport_ping_timeout",
        "venue_down",
        "bybit_sequence_gap",
    ] | None
    assert reason_type == expected_reason_type
    literal_type = next(item for item in get_args(reason_type) if item is not type(None))
    assert frozenset(get_args(literal_type)) == EXPLAINED_FAILURE_REASONS
    reasons = (
        "application_pong_timeout subscription_ack_timeout transport_disconnected "
        "transport_ping_timeout"
    )
    expected_reasons = {*reasons.split(), "venue_down", "bybit_sequence_gap"}
    assert EXPLAINED_FAILURE_REASONS == expected_reasons
    parameters = inspect.signature(pair_explained_intervals).parameters
    assert list(parameters) == ["points", "window_start_ns", "window_end_ns"]
    kinds = [item.kind for item in parameters.values()]
    assert kinds[1:] == [inspect.Parameter.KEYWORD_ONLY] * 2
    assert _pair([]) == PairingResult((), False)
    result = _pair([
        _point(10, venue="bybit"),
        _point(20, "explained_failure", venue="bybit", reason="bybit_sequence_gap"),
        _point(30, venue="bybit"),
    ])
    assert result == PairingResult(((10, 30),), False)


def test_cross_venue_overlap_and_touching_intervals_merge_without_double_counting() -> None:
    overlap = [
        _point(10), _point(15, venue="bybit"), _point(20, "explained_failure"),
        _point(25, "explained_failure", venue="bybit"), _point(35, venue="bybit"), _point(40),
    ]
    touching = [
        _point(10), _point(20, "explained_failure"), _point(25, "explained_failure"),
        _point(30), _point(40, "explained_failure"), _point(50),
    ]
    assert _pair(overlap) == PairingResult(((10, 40),), False)
    assert _pair(touching) == PairingResult(((10, 50),), False)


def test_equal_timestamps_use_replay_ordinal_and_drop_zero_length_pairs() -> None:
    paired = [_point(10), _point(10, "explained_failure"), _point(10)]
    missing_prior = [_point(10, "explained_failure"), _point(10)]
    assert _pair(paired) == PairingResult((), False)
    assert _pair(missing_prior) == PairingResult((), True)


def test_pre_window_prior_is_clipped_and_old_completed_gap_is_ignored() -> None:
    crossing = [_point(1), _point(5, "explained_failure"), _point(20)]
    old = [_point(1), _point(2, "explained_failure"), _point(3)]
    assert _pair(crossing, start_ns=10, end_ns=30) == PairingResult(((10, 20),), False)
    assert _pair(old, start_ns=10, end_ns=30) == PairingResult((), False)


def test_missing_or_out_of_window_boundaries_are_unexplained() -> None:
    cases = [
        [_point(10), _point(15, "explained_failure"), _point(20)],
        [_point(10), _point(15, "explained_failure"), _point(21)],
        [_point(15, "explained_failure"), _point(18)],
        [_point(10), _point(15, "explained_failure")],
        [_point(1), _point(5, "explained_failure")],
    ]
    assert all(
        _pair(points, start_ns=10, end_ns=20) == PairingResult((), True)
        for points in cases
    )


def test_failures_outside_half_open_window_are_ignored() -> None:
    explained = [_point(10), _point(20, "explained_failure")]
    assert _pair(explained, start_ns=10, end_ns=20) == PairingResult((), False)
    for observed_ns in (9, 20):
        unexplained = [_point(observed_ns, "unexplained_failure")]
        assert _pair(unexplained, start_ns=10, end_ns=20) == PairingResult((), False)


def test_unexplained_point_preserves_other_successfully_paired_intervals() -> None:
    points = [
        _point(1), _point(2, "explained_failure"), _point(4),
        _point(5, "unexplained_failure", venue="bybit"),
    ]
    assert _pair(points, end_ns=10) == PairingResult(((1, 4),), True)


def test_coverage_point_structure_and_combinations_are_closed() -> None:
    point, hl = CoveragePoint, "hyperliquid"
    invalid = [
        point([], 1, "hard_verified", None), point("other", 1, "hard_verified", None),
        point(hl, -1, "hard_verified", None), point(hl, True, "hard_verified", None),
        point(hl, 1.0, "hard_verified", None), point(hl, 1, [], None),
        point(hl, 1, "other", None), point(hl, 1, "explained_failure", None),
        point(hl, 1, "explained_failure", "other"),
        point(hl, 1, "hard_verified", "venue_down"),
        point("bybit", 1, "unexplained_failure", "venue_down"), (hl, 1, "hard_verified", None),
        point(hl, 1, "explained_failure", "bybit_sequence_gap"),
    ]
    for point in invalid:
        with pytest.raises(ValueError):
            _pair([point])


def test_pairing_inputs_are_exact_ordered_and_forward() -> None:
    with pytest.raises(ValueError, match="order"):
        _pair([_point(2), _point(1)])
    invalid = [(False, 1), (0.0, 1), (-1, 1), (0, True), (0, 1.0), (1, 1), (2, 1)]
    for start_ns, end_ns in invalid:
        with pytest.raises(ValueError):
            _pair([], start_ns=start_ns, end_ns=end_ns)


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
