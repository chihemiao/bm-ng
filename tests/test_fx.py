from decimal import Decimal

import pytest

from reconciliation.fx import FxRate, MarkPrice, convert_usdt_to_usdc, naked_notional


def _rate(value="1.001", **changes) -> FxRate:
    values = {
        "base": "USDT",
        "quote": "USDC",
        "rate": Decimal(value) if not isinstance(value, float) else value,
        "observed_ns": 100,
    }
    values.update(changes)
    return FxRate(**values)


def _convert(amount="2.5", **changes):
    values = {"rate": _rate(), "now_ns": 110, "max_age_ns": 10}
    values.update(changes)
    value = Decimal(amount) if isinstance(amount, str) else amount
    return convert_usdt_to_usdc(value, **values)


def _mark(value="50000.25", **changes) -> MarkPrice:
    values = {
        "symbol": "BTC",
        "venue": "hyperliquid",
        "price": Decimal(value) if not isinstance(value, float) else value,
        "quote": "USDC",
        "observed_ns": 100,
    }
    values.update(changes)
    return MarkPrice(**values)


def _notional(delta="0.02", **changes):
    values = {
        "mark": _mark(),
        "symbol": "BTC",
        "expected_quote": "USDC",
        "now_ns": 110,
        "max_age_ns": 10,
    }
    values.update(changes)
    value = Decimal(delta) if isinstance(delta, str) else delta
    return naked_notional(value, **values)


def test_usdt_amount_is_multiplied_by_the_exact_usdt_usdc_rate():
    assert _convert() == Decimal("2.5025")


def test_missing_rate_is_unknown_not_implicit_parity():
    assert _convert(rate=None) is None


@pytest.mark.parametrize("observed_ns", [99, 111])
def test_stale_or_future_rate_is_unknown(observed_ns):
    assert _convert(rate=_rate(observed_ns=observed_ns)) is None


def test_rate_at_the_inclusive_age_limit_is_still_fresh():
    assert _convert(rate=_rate(observed_ns=100), now_ns=110, max_age_ns=10) == Decimal("2.5025")


@pytest.mark.parametrize("value", ["0", "-0.01"])
def test_nonpositive_rate_is_rejected(value):
    with pytest.raises(ValueError):
        _convert(rate=_rate(value))


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["amount", "rate"])
def test_non_finite_decimal_is_a_value_error(field, value):
    changes = {"rate": _rate(value)} if field == "rate" else {}
    amount = Decimal(value) if field == "amount" else Decimal("1")
    with pytest.raises(ValueError) as raised:
        _convert(amount, **changes)
    assert type(raised.value) is ValueError


@pytest.mark.parametrize(
    ("amount", "rate"),
    [(1.0, _rate()), (Decimal("1"), _rate(1.0))],
)
def test_non_decimal_numbers_are_rejected(amount, rate):
    with pytest.raises(TypeError):
        _convert(amount, rate=rate)


@pytest.mark.parametrize(
    "rate",
    [
        _rate(base="USDC"),
        _rate(quote="USDT"),
        _rate(base="USDC", quote="USDT"),
    ],
)
def test_only_the_frozen_usdt_usdc_direction_is_accepted(rate):
    with pytest.raises(ValueError, match="USDT/USDC"):
        _convert(rate=rate)


def test_zero_and_negative_amounts_keep_their_sign_and_knownness():
    assert _convert("0") == Decimal(0)
    assert _convert("-2") == Decimal("-2.002")


def test_conversion_does_not_round_or_quantize_the_decimal_product():
    assert _convert("0.123456789", rate=_rate("0.9987654321")) == Decimal("0.1233043732112635269")


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"now_ns": True}, TypeError),
        ({"max_age_ns": 1.0}, TypeError),
        ({"now_ns": 0}, ValueError),
        ({"max_age_ns": 0}, ValueError),
        ({"rate": _rate(observed_ns=True)}, TypeError),
        ({"rate": _rate(observed_ns=0)}, ValueError),
    ],
)
def test_fx_clock_inputs_are_strictly_positive_integers(changes, error):
    with pytest.raises(error):
        _convert(**changes)


def test_naked_notional_uses_the_exact_mark_product():
    assert _notional() == Decimal("1000.0050")


@pytest.mark.parametrize(
    "changes",
    [{"delta": None}, {"mark": None}],
)
def test_unknown_delta_or_missing_mark_propagates_unknown(changes):
    delta = changes.pop("delta", "0.02")
    assert _notional(delta, **changes) is None


@pytest.mark.parametrize("observed_ns", [99, 111])
def test_stale_or_future_mark_is_unknown(observed_ns):
    assert _notional(mark=_mark(observed_ns=observed_ns)) is None


def test_mark_at_the_inclusive_age_limit_is_still_fresh():
    assert _notional(mark=_mark(observed_ns=100), now_ns=110, max_age_ns=10) == Decimal("1000.0050")


@pytest.mark.parametrize(
    "changes",
    [
        {"mark": _mark(symbol="ETH")},
        {"expected_quote": "USDT"},
    ],
)
def test_symbol_and_expected_quote_must_match_the_mark(changes):
    with pytest.raises(ValueError):
        _notional(**changes)


@pytest.mark.parametrize("value", ["0", "-0.01"])
def test_nonpositive_mark_price_is_rejected(value):
    with pytest.raises(ValueError):
        _notional(mark=_mark(value))


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["delta", "price"])
def test_non_finite_notional_inputs_are_rejected(field, value):
    delta = Decimal(value) if field == "delta" else Decimal("1")
    changes = {"mark": _mark(value)} if field == "price" else {}
    with pytest.raises(ValueError) as raised:
        _notional(delta, **changes)
    assert type(raised.value) is ValueError


@pytest.mark.parametrize(
    ("delta", "mark"),
    [(1.0, _mark()), (Decimal("1"), _mark(1.0))],
)
def test_notional_rejects_non_decimal_numbers(delta, mark):
    with pytest.raises(TypeError):
        _notional(delta, mark=mark)


def test_negative_delta_becomes_positive_notional_and_venue_is_only_evidence():
    expected = Decimal("1000.0050")
    assert _notional("-0.02") == expected
    assert _notional(mark=_mark(venue="bybit")) == expected
    assert _notional("0") == Decimal(0)


def test_notional_does_not_round_or_quantize_the_decimal_product():
    assert _notional("0.123456789", mark=_mark("65432.109876")) == Decimal("8078.038182786148164")


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"now_ns": True}, TypeError),
        ({"max_age_ns": 1.0}, TypeError),
        ({"now_ns": 0}, ValueError),
        ({"max_age_ns": 0}, ValueError),
        ({"mark": _mark(observed_ns=True)}, TypeError),
        ({"mark": _mark(observed_ns=0)}, ValueError),
        ({"mark": _mark(venue="")}, ValueError),
        ({"symbol": ""}, ValueError),
        ({"expected_quote": ""}, ValueError),
    ],
)
def test_mark_contract_inputs_are_strict(changes, error):
    with pytest.raises(error):
        _notional(**changes)
