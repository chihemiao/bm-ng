"""Share Hyperliquid normalization primitives without crossing venue boundaries."""

from collections.abc import Callable, Iterable

from reconciliation.state import canonical_fingerprint

_fingerprint = canonical_fingerprint

COINS = frozenset({"BTC", "ETH"})
HL_UINT64_MAX = 2**64 - 1


def _valid_observed_ns(observed_ns: object) -> int:
    if type(observed_ns) is not int:
        raise TypeError("observed_ns must be an integer")
    if observed_ns <= 0:
        raise ValueError("observed_ns must be positive")
    return observed_ns


def _canonical_rows(
    rows: Iterable[object], normalize: Callable[[object], tuple[str, str] | None]
) -> tuple[dict[str, str], int, int]:
    states: dict[str, str] = {}
    unknown = mismatch = 0
    for row in rows:
        parsed = normalize(row)
        if parsed is None:
            unknown += 1
            continue
        state, identity = parsed
        if identity in states:
            unknown += 1
            mismatch += 1
            continue
        states[identity] = state
    return states, unknown, mismatch
