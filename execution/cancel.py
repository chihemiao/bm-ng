"""Authorize and attempt both venue cancellation commands."""

from collections.abc import Callable
from dataclasses import dataclass

from execution.writer import WriterLease

HL_UINT64_MAX = 2**64 - 1
HL_CANCEL_COINS = frozenset({"BTC", "ETH"})


@dataclass(frozen=True, slots=True, kw_only=True)
class HLCancelTarget:
    coin: str
    oid: int


@dataclass(frozen=True, slots=True, kw_only=True)
class HLCancelBatch:
    targets: tuple[HLCancelTarget, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class BybitCancelScope:
    category: str
    settle_coin: str

    def __post_init__(self) -> None:
        for name, value in (("category", self.category), ("settle_coin", self.settle_coin)):
            if type(value) is not str:
                raise TypeError(f"{name} must be a string")
        if self.category != "linear":
            raise ValueError("category must be linear")
        if self.settle_coin != "USDT":
            raise ValueError("settle_coin must be USDT")


@dataclass(frozen=True, slots=True, kw_only=True)
class PairCancelOutcome:
    hyperliquid: object | BaseException
    bybit: object | BaseException


def build_hl_cancel_batch(payload: object) -> HLCancelBatch:
    """Build cancel targets from the action's minimal sufficient wire fields."""
    if type(payload) is not list:
        raise TypeError("HL open-orders payload must be a list")
    targets = []
    for row in payload:
        # Unlike evidence parsing, cancellation needs only these two wire fields.
        if type(row) is not dict:
            raise TypeError("HL open-orders row must be a dict")
        if "coin" not in row or "oid" not in row:
            raise ValueError("HL open-orders row missing coin or oid")
        coin, oid = row["coin"], row["oid"]
        if type(coin) is not str:
            raise TypeError("HL open-orders coin must be a string")
        if coin not in HL_CANCEL_COINS:
            raise ValueError("HL open-orders row has unsupported coin")
        if type(oid) is not int:
            raise TypeError("HL open-orders oid must be an integer")
        if not 0 <= oid <= HL_UINT64_MAX:
            raise ValueError("HL open-orders oid out of uint64 range")
        targets.append(HLCancelTarget(coin=coin, oid=oid))
    return HLCancelBatch(targets=tuple(targets))


def bind_hl_cancel(
    batch: HLCancelBatch, bulk_cancel: Callable[[list[dict[str, object]]], object]
) -> Callable[[], object]:
    """Bind validated targets to one injected Hyperliquid bulk-cancel call."""
    if not isinstance(batch, HLCancelBatch):
        raise TypeError("batch must be HLCancelBatch")
    if not callable(bulk_cancel):
        raise TypeError("bulk_cancel must be callable")

    def transport() -> object:
        rows = [{"coin": target.coin, "oid": target.oid} for target in batch.targets]
        return bulk_cancel(rows)

    return transport


def bind_bybit_cancel(
    scope: BybitCancelScope, cancel_all: Callable[..., object]
) -> Callable[[], object]:
    """Bind the frozen scope to one injected Bybit cancel-all call."""
    if not isinstance(scope, BybitCancelScope):
        raise TypeError("scope must be BybitCancelScope")
    if not callable(cancel_all):
        raise TypeError("cancel_all must be callable")

    def transport() -> object:
        return cancel_all(category=scope.category, settleCoin=scope.settle_coin)

    return transport


def cancel_pair_orders(
    *,
    lease: WriterLease,
    hyperliquid_transport: Callable[[], object],
    bybit_transport: Callable[[], object],
) -> PairCancelOutcome:
    """Attempt both already-bound venue cancellation commands in fixed order."""
    if not isinstance(lease, WriterLease):
        raise TypeError("lease must be WriterLease")
    if not callable(hyperliquid_transport):
        raise TypeError("hyperliquid_transport must be callable")
    if not callable(bybit_transport):
        raise TypeError("bybit_transport must be callable")
    lease.authorize("cancel_all")

    def run(transport: Callable[[], object]) -> object | BaseException:
        try:
            return transport()
        except BaseException as error:
            return error

    return PairCancelOutcome(
        hyperliquid=run(hyperliquid_transport),
        bybit=run(bybit_transport),
    )
