import ast
import importlib
import inspect
import textwrap
from decimal import Decimal

import pytest

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
