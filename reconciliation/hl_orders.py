"""Normalize documented Hyperliquid open-order snapshots."""

from collections.abc import Mapping

from reconciliation.hl_common import COINS, _canonical_rows, _fingerprint, _valid_observed_ns
from reconciliation.state import CanonicalSet, SurfaceEvidence

ORDER_FIELDS = frozenset({"coin", "limitPx", "oid", "side", "sz", "timestamp"})


def _order_row(row: object) -> tuple[str, str] | None:
    # A/B are validated API notation only; direction semantics are not derived here.
    if not isinstance(row, Mapping) or set(row) != ORDER_FIELDS:
        return None
    oid = row["oid"]
    valid = row["coin"] in COINS and row["side"] in {"A", "B"}
    if not valid or type(oid) is not int or oid < 0:
        return None
    try:
        return _fingerprint(row), _fingerprint({"oid": oid})
    except (TypeError, ValueError):
        return None


def parse_orders_surface(payload: list[object], *, observed_ns: int) -> SurfaceEvidence:
    """Parse open orders without deriving trade-direction semantics."""
    if not isinstance(payload, list):
        raise TypeError("payload must be a list")
    _valid_observed_ns(observed_ns)
    states, unknown, mismatch = _canonical_rows(payload, _order_row)
    # The 1000 minimum account limit is a conservative completeness inference.
    truncated = len(payload) >= 1000
    return SurfaceEvidence(
        observed_ns=observed_ns,
        fetched_count=len(payload),
        page_complete=not truncated,
        truncated=truncated,
        unknown_count=unknown,
        mismatch_count=mismatch,
        entities=CanonicalSet("hyperliquid.orders.state", 1, frozenset(states.values())),
        identities=CanonicalSet("hyperliquid.orders.identity", 1, frozenset(states)),
    )
