import importlib
import inspect
from decimal import Decimal

import pytest

from reconciliation.fx import MarkPrice

NAMES = ("SOL", "DOGE", "HYPE", "BTC", "ETH")
PRICES = ("150", "0.2", "40", "64000.25", "3200.5")


def _module():
    return importlib.import_module("reconciliation.hl_surface")


def _payload():
    universe = [{"name": name, "documentedExtra": index} for index, name in enumerate(NAMES)]
    contexts = [{"markPx": price, "evolvingExtra": index} for index, price in enumerate(PRICES)]
    return [{"universe": universe, "marginTables": [], "collateralToken": 0}, contexts]


def _parse(payload=..., *, symbol="BTC", observed_ns=100):
    value = _payload() if payload is ... else payload
    return _module().parse_hl_mark_price(value, symbol=symbol, observed_ns=observed_ns)


@pytest.mark.parametrize("symbol,price", [("BTC", "64000.25"), ("ETH", "3200.5")])
def test_named_nontrivial_index_builds_truthful_mark_price(symbol, price):
    mark = _parse(symbol=symbol, observed_ns=321)
    assert isinstance(mark, MarkPrice)
    fields = (mark.symbol, mark.venue, mark.price, mark.quote, mark.observed_ns)
    assert fields == (symbol, "hyperliquid", Decimal(price), "USDT", 321)


def test_positive_finite_scientific_mark_price_is_accepted_exactly():
    payload = _payload()
    payload[1][3]["markPx"] = "1E+2"
    assert _parse(payload).price == Decimal("1E+2")


@pytest.mark.parametrize(
    "payload,error,message",
    [(None, TypeError, "payload"), ({}, TypeError, "payload"), ((), TypeError, "payload"),
     ([], ValueError, "two"), ([{}], ValueError, "two"), ([{}, [], {}], ValueError, "two")],
)
def test_top_level_payload_type_and_arity_are_exact(payload, error, message):
    with pytest.raises(error, match=message):
        _parse(payload)


def test_meta_must_be_a_mapping():
    payload = _payload()
    payload[0] = []
    with pytest.raises(TypeError, match="meta"):
        _parse(payload)


def test_universe_is_required():
    payload = _payload()
    payload[0].pop("universe")
    with pytest.raises(ValueError, match="universe"):
        _parse(payload)


@pytest.mark.parametrize("part", ["universe", "contexts"])
def test_aligned_parts_must_be_lists(part):
    payload = _payload()
    if part == "universe":
        payload[0]["universe"] = ()
    else:
        payload[1] = ()
    with pytest.raises(TypeError, match=part):
        _parse(payload)


def test_universe_and_context_lengths_must_match():
    payload = _payload()
    payload[1].pop()
    with pytest.raises(ValueError, match="length"):
        _parse(payload)


@pytest.mark.parametrize(
    "bad,error", [([], TypeError), ({"name": 1}, TypeError), ({"name": ""}, ValueError)]
)
def test_every_universe_entry_has_a_nonempty_string_name(bad, error):
    payload = _payload()
    payload[0]["universe"][-1] = bad
    with pytest.raises(error, match="name|universe"):
        _parse(payload)


def test_missing_target_rejects_an_in_range_decoy_price():
    payload = _payload()
    payload[0]["universe"][3]["name"] = "SOL"
    with pytest.raises(ValueError, match="missing"):
        _parse(payload)


def test_duplicate_target_is_not_resolved_by_taking_first():
    payload = _payload()
    payload[0]["universe"][0]["name"] = "BTC"
    with pytest.raises(ValueError, match="duplicate"):
        _parse(payload)


@pytest.mark.parametrize("value,error", [([], TypeError), ({"markPx": 1}, TypeError)])
def test_target_context_and_mark_type_are_strict(value, error):
    payload = _payload()
    payload[1][3] = value
    with pytest.raises(error, match="context|markPx"):
        _parse(payload)


def test_target_mark_field_is_required():
    payload = _payload()
    payload[1][3].pop("markPx")
    with pytest.raises(ValueError, match="markPx"):
        _parse(payload)


@pytest.mark.parametrize(
    "symbol,error", [(None, TypeError), (1, TypeError), ("", ValueError), ("SOL", ValueError)]
)
def test_symbol_is_a_supported_canonical_asset(symbol, error):
    with pytest.raises(error, match="symbol"):
        _parse(symbol=symbol)


@pytest.mark.parametrize(
    "observed_ns,error", [(True, TypeError), (1.0, TypeError), (0, ValueError), (-1, ValueError)]
)
def test_observed_ns_is_an_exact_positive_integer(observed_ns, error):
    with pytest.raises(error, match="observed_ns"):
        _parse(observed_ns=observed_ns)


def test_signature_does_not_expose_venue_or_quote():
    signature = inspect.signature(_module().parse_hl_mark_price)
    assert tuple(signature.parameters) == ("payload", "symbol", "observed_ns")
