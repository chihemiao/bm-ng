import ast
import importlib
import inspect
import textwrap
from decimal import Decimal

import pytest

from reconciliation.exposure import LegPosition
from reconciliation.state import surface_is_authoritative

POSITION = {
    "coin": "BTC",
    "cumFunding": {"allTime": "1", "sinceChange": "0", "sinceOpen": "0"},
    "entryPx": "100000",
    "leverage": {"rawUsd": "1", "type": "cross", "value": 10},
    "liquidationPx": None,
    "marginUsed": "10",
    "maxLeverage": 40,
    "positionValue": "100",
    "returnOnEquity": "0",
    "szi": "0.001",
    "unrealizedPnl": "0",
}


def _row(**changes):
    return {"position": {**POSITION, **changes}, "type": "oneWay"}


def _payload(*rows):
    return {"assetPositions": list(rows), "time": 1}


def _module():
    return importlib.import_module("reconciliation.hl_positions")


def _build(payload=None, *, symbol="BTC", observed_ns=100):
    value = _payload(_row()) if payload is None else payload
    return _module().build_hl_leg_position(value, symbol=symbol, observed_ns=observed_ns)


def test_documented_position_builds_all_leg_fields_from_one_snapshot():
    payload = _payload(_row())
    leg = _build(payload, observed_ns=321)

    assert isinstance(leg, LegPosition)
    assert (leg.venue, leg.symbol, leg.signed_quantity) == (
        "hyperliquid", "BTC", Decimal("0.001"),
    )
    assert leg.evidence == _module().parse_positions_surface(payload, observed_ns=321)


@pytest.mark.parametrize("szi", ["-0.001", "1E+2"])
def test_signed_size_preserves_short_sign_and_exact_scientific_value(szi):
    assert _build(_payload(_row(szi=szi))).signed_quantity == Decimal(szi)


@pytest.mark.parametrize("payload", [_payload(), _payload(_row(coin="ETH"))])
def test_absent_target_symbol_is_authoritative_zero(payload):
    leg = _build(payload)

    assert leg.signed_quantity == Decimal(0)
    assert surface_is_authoritative(leg.evidence, now_ns=100, max_age_ns=1)


def test_present_but_unusable_size_is_zero_with_non_authoritative_evidence():
    leg = _build(_payload(_row(szi="NaN")))

    assert leg.signed_quantity == Decimal(0)
    assert leg.evidence.unknown_count == 1
    assert not surface_is_authoritative(leg.evidence, now_ns=100, max_age_ns=1)


def test_duplicate_coin_keeps_first_quantity_and_makes_evidence_non_authoritative():
    leg = _build(_payload(_row(szi="1"), _row(szi="2")))

    assert leg.signed_quantity == Decimal("1")
    assert (leg.evidence.unknown_count, leg.evidence.mismatch_count) == (1, 1)
    assert not surface_is_authoritative(leg.evidence, now_ns=100, max_age_ns=1)


@pytest.mark.parametrize(
    "symbol,error", [(None, TypeError), (1, TypeError), ("", ValueError), ("SOL", ValueError)]
)
def test_symbol_is_a_supported_nonempty_string(symbol, error):
    with pytest.raises(error, match="symbol"):
        _build(symbol=symbol)


def test_public_signature_has_no_caller_controlled_venue_or_evidence():
    signature = inspect.signature(_module().build_hl_leg_position)
    assert tuple(signature.parameters) == ("payload", "symbol", "observed_ns")


def test_constructor_source_calls_the_shared_position_parser():
    source = textwrap.dedent(inspect.getsource(_module().build_hl_leg_position))
    calls = [node.func.id for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert "parse_positions_surface" in calls


@pytest.mark.parametrize("payload", [[], {}, {"assetPositions": {}}])
def test_position_payload_contract_errors_propagate(payload):
    error = TypeError if isinstance(payload, list) else ValueError
    with pytest.raises(error):
        _build(payload)
