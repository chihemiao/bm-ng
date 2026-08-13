"""Normalize documented Hyperliquid fill-history snapshots."""

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from reconciliation.hl_common import COINS, _fingerprint, _valid_observed_ns
from reconciliation.state import CanonicalSet, SurfaceEvidence

FILL_REQUIRED_FIELDS = frozenset(
    {
        "closedPnl", "coin", "crossed", "dir", "fee", "feeToken", "hash", "oid",
        "px", "side", "startPosition", "sz", "tid", "time",
    }
)
FILL_OPTIONAL_FIELDS = frozenset({"builderFee", "liquidation"})


def _fill_row(row: object) -> tuple[str, str] | None:
    if not isinstance(row, Mapping):
        return None
    fields = set(row)
    valid_fields = FILL_REQUIRED_FIELDS <= fields <= FILL_REQUIRED_FIELDS | FILL_OPTIONAL_FIELDS
    tid, time = row.get("tid"), row.get("time")
    valid_identity = row.get("coin") in COINS and all(type(value) is int for value in (tid, time))
    if not valid_fields or not valid_identity or tid < 0 or time < 0:
        return None
    size = row["sz"]
    if not isinstance(size, str) or not size:
        return None
    try:
        parsed_size = Decimal(size)
    except InvalidOperation:
        return None
    # Sz is an unsigned base-coin size; direction is carried separately by side.
    # Zero remains known because the upstream fill contract does not forbid it.
    if not parsed_size.is_finite() or parsed_size < 0:
        return None
    identity = {"time": time, "coin": row["coin"], "tid": tid}
    try:
        return _fingerprint(row), _fingerprint(identity)
    except (TypeError, ValueError):
        return None


def parse_fills_surface(
    pages: Sequence[list[object]], *, observed_ns: int, page_complete: bool, truncated: bool
) -> SurfaceEvidence:
    """Fold caller-bounded userFillsByTime pages into immutable fill evidence."""
    if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes)):
        raise TypeError("pages must be a sequence")
    if not pages:
        raise ValueError("pages must not be empty")
    if not all(isinstance(page, list) for page in pages):
        raise TypeError("pages must contain lists")
    _valid_observed_ns(observed_ns)
    for name, value in (("page_complete", page_complete), ("truncated", truncated)):
        if type(value) is not bool:
            raise TypeError(f"{name} must be a boolean")

    states: dict[str, str] = {}
    unknown = mismatch = 0
    for row in (row for page in pages for row in page):
        parsed = _fill_row(row)
        if parsed is None:
            unknown += 1
            continue
        state, identity = parsed
        previous = states.get(identity)
        if previous is not None:
            # This comparison is the collision defense for HL's 50-bit tid component.
            mismatch += previous != state
            continue
        states[identity] = state

    # The authenticated testnet account was empty on 2026-08-13; non-empty row
    # shape and cross-page behavior are therefore pinned from official examples.
    return SurfaceEvidence(
        observed_ns=observed_ns,
        fetched_count=len(states) + unknown,
        page_complete=page_complete,
        truncated=truncated,
        unknown_count=unknown,
        mismatch_count=mismatch,
        entities=CanonicalSet("hyperliquid.fills.state", 1, frozenset(states.values())),
        identities=CanonicalSet("hyperliquid.fills.identity", 1, frozenset(states)),
    )
