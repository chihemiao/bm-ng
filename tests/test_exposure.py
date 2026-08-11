from dataclasses import replace
from decimal import Decimal

import pytest

from reconciliation.exposure import LegPosition, net_delta
from reconciliation.state import CanonicalSet, SurfaceEvidence


def _canonical(kind: str) -> CanonicalSet:
    return CanonicalSet(f"positions.{kind}", 1, frozenset({kind}))


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


def _leg(venue: str, quantity: object, **changes) -> LegPosition:
    values = {
        "venue": venue,
        "symbol": "BTC",
        "signed_quantity": quantity,
        "evidence": _evidence(),
    }
    values.update(changes)
    return LegPosition(**values)


def _positions(left="-0.1", right="0.1") -> list[LegPosition]:
    return [
        _leg("hyperliquid", Decimal(left)),
        _leg("bybit", Decimal(right)),
    ]


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [("-0.1", "0.1", "0.0"), ("-0.1", "0.08", "-0.02")],
)
def test_two_authoritative_legs_sum_with_exact_decimal_arithmetic(
    left, right, expected
):
    assert net_delta(_positions(left, right), symbol="BTC", now_ns=110, max_age_ns=10) == Decimal(
        expected
    )


@pytest.mark.parametrize("shape", ["missing", "duplicate", "unknown"])
def test_venue_set_must_be_exactly_the_two_frozen_venues(shape):
    positions = _positions()
    if shape == "missing":
        positions.pop()
    elif shape == "duplicate":
        positions[1] = _leg("hyperliquid", Decimal("0.1"))
    else:
        positions[1] = _leg("other", Decimal("0.1"))
    with pytest.raises(ValueError, match="venue"):
        net_delta(positions, symbol="BTC", now_ns=110, max_age_ns=10)


def test_every_leg_must_match_the_requested_symbol():
    positions = _positions()
    positions[1] = replace(positions[1], symbol="ETH")
    with pytest.raises(ValueError, match="symbol"):
        net_delta(positions, symbol="BTC", now_ns=110, max_age_ns=10)


@pytest.mark.parametrize(
    "changes",
    [
        {"page_complete": False},
        {"truncated": True},
        {"unknown_count": 1, "fetched_count": 2},
        {"mismatch_count": 1},
    ],
)
def test_each_non_authoritative_surface_condition_makes_delta_unknown(changes):
    positions = _positions()
    positions[0] = replace(positions[0], evidence=_evidence(**changes))
    assert net_delta(positions, symbol="BTC", now_ns=110, max_age_ns=10) is None


@pytest.mark.parametrize("observed_ns", [99, 111])
def test_stale_or_future_surface_makes_delta_unknown(observed_ns):
    positions = _positions()
    positions[0] = replace(positions[0], evidence=_evidence(observed_ns=observed_ns))
    assert net_delta(positions, symbol="BTC", now_ns=110, max_age_ns=10) is None


@pytest.mark.parametrize("quantity", [0.1, 1])
def test_quantities_must_be_exact_decimal_instances(quantity):
    positions = _positions()
    positions[0] = replace(positions[0], signed_quantity=quantity)
    with pytest.raises(TypeError, match="signed_quantity"):
        net_delta(positions, symbol="BTC", now_ns=110, max_age_ns=10)


def test_malformed_embedded_surface_is_rejected_not_made_unknown():
    positions = _positions()
    positions[0] = replace(positions[0], evidence=_evidence(fetched_count=2))
    with pytest.raises(ValueError, match="fetched_count"):
        net_delta(positions, symbol="BTC", now_ns=110, max_age_ns=10)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("now_ns", True, TypeError),
        ("max_age_ns", 1.0, TypeError),
        ("now_ns", 0, ValueError),
        ("max_age_ns", 0, ValueError),
    ],
)
def test_clock_inputs_are_strictly_positive_integers(field, value, error):
    values = {"symbol": "BTC", "now_ns": 110, "max_age_ns": 10}
    values[field] = value
    with pytest.raises(error, match=field):
        net_delta(_positions(), **values)
