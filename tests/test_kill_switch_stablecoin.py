from decimal import Decimal

import pytest

from reconciliation.fx import FxRate
from reconciliation.kill_switch import stablecoin_spread_known, stablecoin_spread_trigger


def _rate(value="1", **changes) -> FxRate:
    values = {
        "base": "USDT",
        "quote": "USDC",
        "rate": Decimal(value) if isinstance(value, str) else value,
        "observed_ns": 100,
    }
    return FxRate(**(values | changes))


DEFAULT_RATE = _rate()


def _known(rate=DEFAULT_RATE, **changes) -> bool:
    values = {"now_ns": 110, "max_age_ns": 10}
    return stablecoin_spread_known(rate, **(values | changes))


def _trigger(rate=DEFAULT_RATE, **changes) -> bool:
    values = {
        "now_ns": 110,
        "max_age_ns": 10,
        "max_abs_deviation": Decimal("0.01"),
    }
    return stablecoin_spread_trigger(rate, **(values | changes))


def test_missing_rate_is_unknown_and_defensively_triggers() -> None:
    assert _known(None) is False
    assert _trigger(None) is True


@pytest.mark.parametrize(
    ("observed_ns", "known", "triggered"),
    [(100, True, False), (99, False, True), (111, False, True)],
)
def test_rate_freshness_has_an_inclusive_age_limit_and_rejects_future_data(
    observed_ns, known, triggered,
) -> None:
    rate = _rate(observed_ns=observed_ns)
    assert _known(rate) is known
    assert _trigger(rate) is triggered


@pytest.mark.parametrize(
    ("rate", "triggered"),
    [
        ("1", False),
        ("1.01", False),
        ("0.99", False),
        ("1.0101", True),
        ("0.9899", True),
    ],
)
def test_absolute_deviation_uses_strict_symmetric_threshold(rate, triggered) -> None:
    assert _known(_rate(rate)) is True
    assert _trigger(_rate(rate)) is triggered


def test_zero_threshold_allows_only_exact_parity() -> None:
    assert _trigger(_rate("1"), max_abs_deviation=Decimal(0)) is False
    assert _trigger(_rate("1.0001"), max_abs_deviation=Decimal(0)) is True


@pytest.mark.parametrize("rate", [object(), Decimal("1")])
@pytest.mark.parametrize("function", [_known, _trigger])
def test_rate_requires_the_frozen_fx_value_object(function, rate) -> None:
    with pytest.raises(TypeError, match="rate"):
        function(rate)


@pytest.mark.parametrize(
    "rate",
    [
        _rate(base="USDC"),
        _rate("0"),
        _rate("NaN"),
        _rate(observed_ns=True),
    ],
)
@pytest.mark.parametrize("function", [_known, _trigger])
def test_rate_contract_is_validated_before_freshness(function, rate) -> None:
    with pytest.raises((TypeError, ValueError)):
        function(rate)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"now_ns": True}, TypeError),
        ({"max_age_ns": 1.0}, TypeError),
        ({"now_ns": 0}, ValueError),
        ({"max_age_ns": 0}, ValueError),
    ],
)
@pytest.mark.parametrize("function", [_known, _trigger])
def test_clock_inputs_are_strictly_positive_integers(function, changes, error) -> None:
    with pytest.raises(error):
        function(**changes)


@pytest.mark.parametrize(
    ("threshold", "error"),
    [
        (0.01, TypeError),
        (Decimal("-0.01"), ValueError),
        (Decimal("NaN"), ValueError),
        (Decimal("Infinity"), ValueError),
    ],
)
def test_deviation_threshold_is_a_finite_nonnegative_decimal(threshold, error) -> None:
    with pytest.raises(error, match="max_abs_deviation"):
        _trigger(max_abs_deviation=threshold)


def test_missing_rate_does_not_hide_an_invalid_threshold() -> None:
    with pytest.raises(TypeError, match="max_abs_deviation"):
        _trigger(None, max_abs_deviation=0.01)
