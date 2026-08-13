"""Compose venue position snapshots into fail-closed base-asset exposure."""

from decimal import Decimal

from reconciliation.bybit_surface import build_bybit_leg_position
from reconciliation.exposure import net_delta
from reconciliation.fx import Notional, naked_notional
from reconciliation.hl_surface import (
    COINS,
    build_hl_leg_position,
    parse_hl_mark_price,
)


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def build_net_delta(
    *,
    hl_payload: object,
    hl_observed_ns: int,
    bybit_payload: object,
    bybit_observed_ns: int,
    symbol: str,
    now_ns: int,
    max_age_ns: int,
) -> Decimal | None:
    """Build both venue legs and add their documented base-asset quantities."""
    hl_leg = build_hl_leg_position(hl_payload, symbol=symbol, observed_ns=hl_observed_ns)
    bybit_leg = build_bybit_leg_position(
        bybit_payload, symbol=symbol, observed_ns=bybit_observed_ns
    )
    # Retrieved 2026-08-13. These docs establish base-coin units but no runtime
    # multiplier guard; a future BTC/ETH contract multiplier could silently break this sum.
    # hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/notation
    # bybit.com/en/help-center/article?id=000001060&language=en_US
    # bybit-exchange.github.io/docs/v5/market/instrument
    return net_delta(
        (hl_leg, bybit_leg), symbol=symbol, now_ns=now_ns, max_age_ns=max_age_ns
    )


def build_naked_notional(
    *,
    hl_payload: object,
    hl_observed_ns: int,
    bybit_payload: object,
    bybit_observed_ns: int,
    mark_payload: list[object],
    mark_observed_ns: int,
    symbol: str,
    now_ns: int,
    position_max_age_ns: int,
    mark_max_age_ns: int,
) -> Notional | None:
    """Build fresh venue exposure and value it with the matching HL mark."""
    if type(symbol) is not str:
        raise TypeError("symbol must be a string")
    if symbol not in COINS:
        raise ValueError("symbol must be BTC or ETH")
    for value, name in (
        (hl_observed_ns, "hl_observed_ns"),
        (bybit_observed_ns, "bybit_observed_ns"),
        (mark_observed_ns, "mark_observed_ns"),
        (now_ns, "now_ns"),
        (position_max_age_ns, "position_max_age_ns"),
        (mark_max_age_ns, "mark_max_age_ns"),
    ):
        _positive_int(value, name)
    mark = parse_hl_mark_price(mark_payload, symbol=symbol, observed_ns=mark_observed_ns)
    delta = build_net_delta(
        hl_payload=hl_payload,
        hl_observed_ns=hl_observed_ns,
        bybit_payload=bybit_payload,
        bybit_observed_ns=bybit_observed_ns,
        symbol=symbol,
        now_ns=now_ns,
        max_age_ns=position_max_age_ns,
    )
    return naked_notional(
        delta,
        mark=mark,
        symbol=symbol,
        expected_quote="USDT",
        now_ns=now_ns,
        max_age_ns=mark_max_age_ns,
    )
