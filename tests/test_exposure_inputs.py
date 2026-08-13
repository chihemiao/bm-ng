import ast
import importlib
import inspect
import textwrap
from decimal import Decimal

import pytest

from reconciliation.fx import Notional

HL_POSITION = {
    "coin": "BTC",
    "cumFunding": {},
    "entryPx": "1",
    "leverage": {},
    "liquidationPx": None,
    "marginUsed": "1",
    "maxLeverage": 40,
    "positionValue": "1",
    "returnOnEquity": "0",
    "szi": "1.5",
    "unrealizedPnl": "0",
}
BYBIT_ROW = {"positionIdx": 0, "symbol": "BTCUSDT", "side": "Sell", "size": "1.5"}


def _module():
    return importlib.import_module("reconciliation.exposure_inputs")


def _hl_payload(size="1.5"):
    return {"assetPositions": [{"position": {**HL_POSITION, "szi": size}, "type": "oneWay"}]}


def _bybit_payload(*rows):
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"category": "linear", "nextPageCursor": "", "list": list(rows)},
        "retExtInfo": {},
        "time": 1,
    }


def _mark_payload(price="64000.25"):
    return [
        {"universe": [{"name": "ETH"}, {"name": "BTC"}]},
        [{"markPx": "3200.5"}, {"markPx": price}],
    ]


def _build(**changes):
    values = {
        "hl_payload": _hl_payload(),
        "hl_observed_ns": 100,
        "bybit_payload": _bybit_payload(BYBIT_ROW),
        "bybit_observed_ns": 100,
        "symbol": "BTC",
        "now_ns": 110,
        "max_age_ns": 10,
    }
    values.update(changes)
    return _module().build_net_delta(**values)


def _notional(**changes):
    values = {
        "hl_payload": _hl_payload(),
        "hl_observed_ns": 100,
        "bybit_payload": _bybit_payload({**BYBIT_ROW, "size": "1.25"}),
        "bybit_observed_ns": 100,
        "mark_payload": _mark_payload(),
        "mark_observed_ns": 100,
        "symbol": "BTC",
        "now_ns": 110,
        "position_max_age_ns": 10,
        "mark_max_age_ns": 10,
    }
    values.update(changes)
    return _module().build_naked_notional(**values)


def test_equal_opposite_base_quantities_build_exact_zero_delta():
    assert _build() == Decimal(0)


def test_imbalanced_base_quantities_preserve_exact_decimal_difference():
    row = {**BYBIT_ROW, "size": "1.25"}
    assert _build(bybit_payload=_bybit_payload(row)) == Decimal("0.25")


def test_two_long_legs_are_added_without_inventing_a_venue_sign_flip():
    row = {**BYBIT_ROW, "side": "Buy", "size": "1.0"}
    assert _build(hl_payload=_hl_payload("1.0"), bybit_payload=_bybit_payload(row)) == Decimal(
        "2.0"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"hl_payload": _hl_payload("NaN")},
        {"bybit_payload": _bybit_payload()},
        {"hl_observed_ns": 99},
        {"bybit_observed_ns": 99},
    ],
)
def test_unknown_or_stale_single_leg_propagates_unknown(changes):
    assert _build(**changes) is None


@pytest.mark.parametrize("symbol", ["", "SOL"])
def test_unsupported_symbol_is_rejected(symbol):
    with pytest.raises(ValueError, match="symbol"):
        _build(symbol=symbol)


@pytest.mark.parametrize(
    "changes,error",
    [({"hl_payload": []}, TypeError), ({"bybit_payload": []}, TypeError)],
)
def test_each_venue_payload_contract_error_propagates(changes, error):
    with pytest.raises(error):
        _build(**changes)


def test_signature_keeps_independent_observation_times_and_no_extra_controls():
    signature = inspect.signature(_module().build_net_delta)
    assert tuple(signature.parameters) == (
        "hl_payload",
        "hl_observed_ns",
        "bybit_payload",
        "bybit_observed_ns",
        "symbol",
        "now_ns",
        "max_age_ns",
    )
    assert all(
        value.kind is inspect.Parameter.KEYWORD_ONLY for value in signature.parameters.values()
    )


def test_composer_directly_calls_both_builders_and_venue_neutral_delta():
    source = textwrap.dedent(inspect.getsource(_module().build_net_delta))
    calls = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"build_hl_leg_position", "build_bybit_leg_position", "net_delta"} <= calls


def test_naked_notional_composer_returns_exact_usdt_value():
    result = _notional()
    assert result == Notional(Decimal("16000.0625"), "USDT")
    assert result.quote == _module().parse_hl_mark_price(
        _mark_payload(), symbol="BTC", observed_ns=100
    ).quote


@pytest.mark.parametrize(
    "changes,amount",
    [
        ({"hl_payload": _hl_payload("1.0")}, Decimal("16000.0625")),
        ({"bybit_payload": _bybit_payload(BYBIT_ROW)}, Decimal(0)),
    ],
)
def test_naked_notional_is_unsigned_for_negative_and_zero_delta(changes, amount):
    assert _notional(**changes) == Notional(amount, "USDT")


@pytest.mark.parametrize(
    "changes",
    [
        {"hl_payload": _hl_payload("NaN")},
        {"bybit_payload": _bybit_payload()},
        {"hl_observed_ns": 99},
        {"bybit_observed_ns": 99},
        {"mark_observed_ns": 99},
    ],
)
def test_naked_notional_propagates_unknown_surfaces(changes):
    assert _notional(**changes) is None


def test_bad_mark_raises_before_an_unknown_delta_can_short_circuit_it():
    with pytest.raises(ValueError, match="markPx"):
        _notional(hl_payload=_hl_payload("NaN"), mark_payload=_mark_payload("bad"))


def test_bad_position_payload_error_propagates():
    with pytest.raises(TypeError, match="hl_payload|payload"):
        _notional(hl_payload=[])


@pytest.mark.parametrize("symbol", ["", "SOL"])
def test_naked_notional_rejects_unsupported_symbol(symbol):
    with pytest.raises(ValueError, match="symbol"):
        _notional(symbol=symbol)


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("hl_observed_ns", True, TypeError),
        ("bybit_observed_ns", 0, ValueError),
        ("mark_observed_ns", 1.0, TypeError),
        ("position_max_age_ns", 0, ValueError),
        ("mark_max_age_ns", True, TypeError),
        ("now_ns", 0, ValueError),
    ],
)
def test_naked_notional_clocks_are_strict_positive_integers(field, value, error):
    with pytest.raises(error, match=field):
        _notional(**{field: value})


def test_position_and_mark_freshness_limits_are_not_interchanged():
    changes = {
        "hl_observed_ns": 100,
        "bybit_observed_ns": 100,
        "mark_observed_ns": 90,
        "now_ns": 110,
    }
    assert _notional(**changes, position_max_age_ns=10, mark_max_age_ns=20) is not None
    assert _notional(**changes, position_max_age_ns=20, mark_max_age_ns=10) is None


def test_naked_notional_signature_has_only_the_frozen_keyword_inputs():
    signature = inspect.signature(_module().build_naked_notional)
    assert tuple(signature.parameters) == (
        "hl_payload", "hl_observed_ns", "bybit_payload", "bybit_observed_ns",
        "mark_payload", "mark_observed_ns", "symbol", "now_ns",
        "position_max_age_ns", "mark_max_age_ns",
    )
    assert all(
        value.kind is inspect.Parameter.KEYWORD_ONLY for value in signature.parameters.values()
    )


def test_notional_composer_calls_the_frozen_parser_delta_and_valuator():
    source = textwrap.dedent(inspect.getsource(_module().build_naked_notional))
    calls = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"parse_hl_mark_price", "build_net_delta", "naked_notional"} <= calls
