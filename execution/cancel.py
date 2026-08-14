"""Authorize and attempt both venue cancellation commands."""

from collections.abc import Callable
from dataclasses import dataclass

from execution.writer import WriterLease


@dataclass(frozen=True, slots=True, kw_only=True)
class PairCancelOutcome:
    hyperliquid: object | BaseException
    bybit: object | BaseException


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
