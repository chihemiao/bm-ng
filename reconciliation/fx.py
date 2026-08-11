"""Fail-closed USDT to USDC risk conversion."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class FxRate:
    base: str
    quote: str
    rate: Decimal
    observed_ns: int


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _validate_rate(value: FxRate) -> None:
    if not isinstance(value, FxRate):
        raise TypeError("rate must be FxRate or None")
    if type(value.base) is not str or type(value.quote) is not str:
        raise TypeError("rate currencies must be strings")
    if (value.base, value.quote) != ("USDT", "USDC"):
        raise ValueError("rate must be USDT/USDC")
    if type(value.rate) is not Decimal:
        raise TypeError("rate value must be Decimal")
    if not value.rate.is_finite():
        raise ValueError("rate value must be finite")
    if value.rate <= 0:
        raise ValueError("rate value must be positive")
    _positive_int(value.observed_ns, "observed_ns")


def convert_usdt_to_usdc(
    amount_usdt: Decimal,
    *,
    rate: FxRate | None,
    now_ns: int,
    max_age_ns: int,
) -> Decimal | None:
    """Convert exact USDT amount, or None when no fresh direct quote exists."""
    if type(amount_usdt) is not Decimal:
        raise TypeError("amount_usdt must be Decimal")
    if not amount_usdt.is_finite():
        raise ValueError("amount_usdt must be finite")
    now = _positive_int(now_ns, "now_ns")
    max_age = _positive_int(max_age_ns, "max_age_ns")
    if rate is None:
        return None
    _validate_rate(rate)
    age_ns = now - rate.observed_ns
    if not 0 <= age_ns <= max_age:
        return None
    return amount_usdt * rate.rate
