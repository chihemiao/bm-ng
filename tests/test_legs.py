from decimal import Decimal

import pytest

import reconciliation.exposure as exposure_module
import reconciliation.legs as legs_module
from reconciliation.legs import LegOutcome, PairState, leg_completion, pair_state
from reconciliation.state import CanonicalSet, SurfaceEvidence, surface_is_authoritative


def _canonical(kind: str) -> CanonicalSet:
    return CanonicalSet(f"fills.{kind}", 1, frozenset({kind}))


def _evidence(**changes) -> SurfaceEvidence:
    values = {
        "observed_ns": 100,
        "fetched_count": 1,
        "page_complete": True,
        "truncated": False,
        "unknown_count": 0,
        "mismatch_count": 0,
        "entities": _canonical("state"),
        "identities": _canonical("identity"),
    }
    values.update(changes)
    return SurfaceEvidence(**values)


def _completion(filled, **changes) -> str:
    values = {
        "intended_quantity": Decimal("1"),
        "filled_quantity": None if filled is None else Decimal(filled),
        "evidence": _evidence(),
        "now_ns": 110,
        "max_age_ns": 10,
    }
    values.update(changes)
    return leg_completion(**values)


@pytest.mark.parametrize(
    ("filled", "expected"),
    [("0", "none"), ("0.4", "partial"), ("1", "complete"), ("1.1", "overfilled")],
)
def test_authoritative_fill_quantity_has_a_closed_completion_state(filled, expected):
    assert _completion(filled) == expected


def test_missing_filled_quantity_is_unknown_not_none_filled():
    assert _completion(None) == "unknown"


@pytest.mark.parametrize(
    ("changes", "now_ns", "max_age_ns"),
    [
        ({"page_complete": False}, 110, 10),
        ({"truncated": True}, 110, 10),
        ({"unknown_count": 1, "fetched_count": 2}, 110, 10),
        ({"mismatch_count": 1}, 110, 10),
        ({"observed_ns": 99}, 110, 10),
        ({"observed_ns": 111}, 110, 10),
    ],
)
def test_non_authoritative_or_nonfresh_fill_surface_is_unknown(changes, now_ns, max_age_ns):
    assert (
        _completion("1", evidence=_evidence(**changes), now_ns=now_ns, max_age_ns=max_age_ns)
        == "unknown"
    )


def test_non_authoritative_exact_fill_is_unknown_before_arithmetic():
    assert _completion("1", evidence=_evidence(truncated=True)) == "unknown"


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["intended_quantity", "filled_quantity"])
def test_non_finite_quantities_are_value_errors(field, value):
    with pytest.raises(ValueError) as raised:
        _completion("0", **{field: Decimal(value)})
    assert type(raised.value) is ValueError


@pytest.mark.parametrize(
    "changes",
    [
        {"intended_quantity": Decimal("0")},
        {"intended_quantity": Decimal("-1")},
        {"filled_quantity": Decimal("-0.1")},
    ],
)
def test_quantity_domains_are_enforced(changes):
    with pytest.raises(ValueError):
        _completion("0", **changes)


@pytest.mark.parametrize(
    "changes",
    [{"intended_quantity": 1.0}, {"filled_quantity": 0.0}],
)
def test_quantities_reject_non_decimal_numbers(changes):
    with pytest.raises(TypeError):
        _completion("0", **changes)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"now_ns": True}, TypeError),
        ({"max_age_ns": 1.0}, TypeError),
        ({"now_ns": 0}, ValueError),
        ({"max_age_ns": 0}, ValueError),
    ],
)
def test_completion_clock_inputs_are_strictly_positive_integers(changes, error):
    with pytest.raises(error):
        _completion("0", **changes)


def test_exposure_and_legs_share_the_same_authoritativeness_predicate():
    assert exposure_module.surface_is_authoritative is surface_is_authoritative
    assert legs_module.surface_is_authoritative is surface_is_authoritative


def _pair(hyperliquid: str, bybit: str) -> list[LegOutcome]:
    return [LegOutcome("hyperliquid", hyperliquid), LegOutcome("bybit", bybit)]


def test_two_complete_legs_are_the_only_fully_resolved_pair():
    assert pair_state(_pair("complete", "complete")) == PairState("balanced", ())


def test_two_unfilled_legs_are_not_misreported_as_a_completed_balance():
    assert pair_state(_pair("none", "none")) == PairState(
        "unfilled", (("bybit", "none"), ("hyperliquid", "none"))
    )


@pytest.mark.parametrize(
    ("hyperliquid", "bybit", "unresolved"),
    [
        ("complete", "none", (("bybit", "none"),)),
        ("complete", "partial", (("bybit", "partial"),)),
        (
            "partial",
            "partial",
            (("bybit", "partial"), ("hyperliquid", "partial")),
        ),
    ],
)
def test_underfilled_pair_shapes_share_one_imbalanced_label(hyperliquid, bybit, unresolved):
    assert pair_state(_pair(hyperliquid, bybit)) == PairState("imbalanced", unresolved)


@pytest.mark.parametrize(
    ("hyperliquid", "bybit", "unresolved"),
    [
        ("overfilled", "complete", (("hyperliquid", "overfilled"),)),
        (
            "overfilled",
            "partial",
            (("bybit", "partial"), ("hyperliquid", "overfilled")),
        ),
    ],
)
def test_known_overfill_has_a_distinct_label_and_remains_unresolved(hyperliquid, bybit, unresolved):
    assert pair_state(_pair(hyperliquid, bybit)) == PairState("overfilled", unresolved)


@pytest.mark.parametrize(
    ("hyperliquid", "bybit", "unresolved"),
    [
        ("unknown", "complete", (("hyperliquid", "unknown"),)),
        (
            "unknown",
            "overfilled",
            (("bybit", "overfilled"), ("hyperliquid", "unknown")),
        ),
        (
            "unknown",
            "unknown",
            (("bybit", "unknown"), ("hyperliquid", "unknown")),
        ),
    ],
)
def test_unknown_label_dominates_without_hiding_known_unresolved_information(
    hyperliquid, bybit, unresolved
):
    assert pair_state(_pair(hyperliquid, bybit)) == PairState("unknown", unresolved)


@pytest.mark.parametrize(
    "legs",
    [
        [LegOutcome("hyperliquid", "complete")],
        [LegOutcome("hyperliquid", "complete"), LegOutcome("hyperliquid", "none")],
        [LegOutcome("hyperliquid", "complete"), LegOutcome("other", "none")],
    ],
)
def test_pair_requires_each_frozen_venue_exactly_once(legs):
    with pytest.raises(ValueError, match="venue"):
        pair_state(legs)


def test_invalid_completion_is_rejected_not_skipped():
    with pytest.raises(ValueError, match="completion"):
        pair_state(_pair("complete", "other"))


@pytest.mark.parametrize(
    "legs",
    [
        [object(), LegOutcome("bybit", "complete")],
        [LegOutcome(1, "complete"), LegOutcome("bybit", "complete")],
        [LegOutcome("hyperliquid", 1), LegOutcome("bybit", "complete")],
    ],
)
def test_pair_rejects_non_outcome_or_non_string_fields(legs):
    with pytest.raises(TypeError):
        pair_state(legs)


def test_unresolved_order_is_deterministic_regardless_of_input_order():
    forward = _pair("unknown", "overfilled")
    assert pair_state(forward) == pair_state(list(reversed(forward)))
