"""Fail-closed currency and mark-price risk valuation."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Notional:
    amount: Decimal
    quote: str

    def __post_init__(self) -> None:
        if type(self.amount) is not Decimal:
            raise TypeError("notional amount must be Decimal")
        if not self.amount.is_finite() or self.amount < 0:
            raise ValueError("notional amount must be finite and nonnegative")
        _nonempty_text(self.quote, "notional quote")


@dataclass(frozen=True, slots=True)
class FxRate:
    base: str
    quote: str
    rate: Decimal
    observed_ns: int


@dataclass(frozen=True, slots=True)
class MarkPrice:
    symbol: str
    venue: str
    price: Decimal
    quote: str
    observed_ns: int


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _nonempty_text(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")
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


def _validate_mark(value: MarkPrice) -> None:
    if not isinstance(value, MarkPrice):
        raise TypeError("mark must be MarkPrice or None")
    _nonempty_text(value.symbol, "mark symbol")
    _nonempty_text(value.venue, "mark venue")
    _nonempty_text(value.quote, "mark quote")
    if type(value.price) is not Decimal:
        raise TypeError("mark price must be Decimal")
    if not value.price.is_finite():
        raise ValueError("mark price must be finite")
    if value.price <= 0:
        raise ValueError("mark price must be positive")
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


def naked_notional(
    delta: Decimal | None,
    *,
    mark: MarkPrice | None,
    symbol: str,
    expected_quote: str,
    now_ns: int,
    max_age_ns: int,
) -> Notional | None:
    """Value absolute base exposure, or None when quantity or price is unknown."""
    if delta is not None and type(delta) is not Decimal:
        raise TypeError("delta must be Decimal or None")
    if delta is not None and not delta.is_finite():
        raise ValueError("delta must be finite")
    expected_symbol = _nonempty_text(symbol, "symbol")
    expected_currency = _nonempty_text(expected_quote, "expected_quote")
    now = _positive_int(now_ns, "now_ns")
    max_age = _positive_int(max_age_ns, "max_age_ns")
    if delta is None or mark is None:
        return None
    _validate_mark(mark)
    if mark.symbol != expected_symbol:
        raise ValueError("mark symbol mismatch")
    if mark.quote != expected_currency:
        raise ValueError("mark quote mismatch")
    age_ns = now - mark.observed_ns
    if not 0 <= age_ns <= max_age:
        return None
    return Notional(abs(delta) * mark.price, mark.quote)
