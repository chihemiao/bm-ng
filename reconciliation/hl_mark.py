"""Normalize documented Hyperliquid mark-price snapshots."""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from reconciliation.fx import MarkPrice
from reconciliation.hl_common import COINS, _valid_observed_ns


def _hl_mark_arrays(payload: object) -> tuple[list[object], list[object]]:
    if type(payload) is not list:
        raise TypeError("payload must be a list")
    if len(payload) != 2:
        raise ValueError("payload must contain two elements")
    meta, contexts = payload
    if not isinstance(meta, Mapping):
        raise TypeError("meta must be a mapping")
    if "universe" not in meta:
        raise ValueError("universe is missing")
    universe = meta["universe"]
    if type(universe) is not list:
        raise TypeError("universe must be a list")
    if type(contexts) is not list:
        raise TypeError("contexts must be a list")
    if len(universe) != len(contexts):
        raise ValueError("universe and context lengths differ")
    return universe, contexts


def _hl_mark_index(universe: list[object], symbol: str) -> int:
    matches = []
    for index, asset in enumerate(universe):
        if not isinstance(asset, Mapping):
            raise TypeError("universe entry must be a mapping")
        name = asset.get("name")
        if type(name) is not str:
            raise TypeError("name must be a string")
        if not name:
            raise ValueError("name must not be empty")
        if name == symbol:
            matches.append(index)
    if not matches:
        raise ValueError("target symbol is missing")
    if len(matches) != 1:
        raise ValueError("target symbol is duplicated")
    return matches[0]


def parse_hl_mark_price(payload: list[object], *, symbol: str, observed_ns: int) -> MarkPrice:
    if type(symbol) is not str:
        raise TypeError("symbol must be a string")
    if symbol not in COINS:
        raise ValueError("symbol must be BTC or ETH")
    _valid_observed_ns(observed_ns)
    universe, contexts = _hl_mark_arrays(payload)
    context = contexts[_hl_mark_index(universe, symbol)]
    if not isinstance(context, Mapping):
        raise TypeError("target context must be a mapping")
    if "markPx" not in context:
        raise ValueError("markPx is missing")
    raw_price = context["markPx"]
    if type(raw_price) is not str:
        raise TypeError("markPx must be a string")
    try:
        price = Decimal(raw_price)
    except InvalidOperation as error:
        raise ValueError("markPx is invalid") from error
    if not price.is_finite() or price <= 0:
        raise ValueError("markPx must be finite and positive")
    # BTC/ETH marks are USDT-denominated; HL's 1:1 USDC quanto settlement is not market FX.
    return MarkPrice(symbol, "hyperliquid", price, "USDT", observed_ns)
