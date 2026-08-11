from decimal import Decimal

import pytest

import reconciliation.exposure as exposure_module
import reconciliation.legs as legs_module
from reconciliation.legs import leg_completion
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
def test_non_authoritative_or_nonfresh_fill_surface_is_unknown(
    changes, now_ns, max_age_ns
):
    assert _completion(
        "1", evidence=_evidence(**changes), now_ns=now_ns, max_age_ns=max_age_ns
    ) == "unknown"


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
