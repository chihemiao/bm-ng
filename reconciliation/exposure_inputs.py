"""Compose venue position snapshots into fail-closed base-asset exposure."""

from decimal import Decimal

from reconciliation.bybit_surface import build_bybit_leg_position
from reconciliation.exposure import net_delta
from reconciliation.hl_surface import build_hl_leg_position


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
