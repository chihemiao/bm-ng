"""Normalize documented Hyperliquid fill-history snapshots."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from data.schema_dispatch import ORDER_SIDES
from reconciliation.hl_common import COINS, _fingerprint, _valid_observed_ns
from reconciliation.state import CanonicalSet, SurfaceEvidence

FILL_REQUIRED_FIELDS = frozenset(
    {
        "closedPnl", "coin", "crossed", "dir", "fee", "feeToken", "hash", "oid",
        "px", "side", "startPosition", "sz", "tid", "time",
    }
)
FILL_OPTIONAL_FIELDS = frozenset({"builderFee", "liquidation"})


@dataclass(frozen=True, slots=True)
class HLFilledQuantity:
    quantity: Decimal | None
    evidence: SurfaceEvidence


def _fill_row(row: object) -> tuple[str, str] | None:
    if not isinstance(row, Mapping):
        return None
    fields = set(row)
    valid_fields = FILL_REQUIRED_FIELDS <= fields <= FILL_REQUIRED_FIELDS | FILL_OPTIONAL_FIELDS
    oid, tid, time = row.get("oid"), row.get("tid"), row.get("time")
    coin, side = row.get("coin"), row.get("side")
    valid_identity = isinstance(coin, str) and coin in COINS
    valid_identity &= isinstance(side, str) and side in {"A", "B"}
    valid_identity &= all(type(value) is int for value in (oid, tid, time))
    if not valid_fields or not valid_identity or min(oid, tid, time) < 0:
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


def _nonnegative_int(value: object, name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def build_hl_filled_quantity(
    pages: Sequence[list[object]], *, coin: str, intended_side: str,
    oids: frozenset[int], since_ms: int, skew_allowance_ms: int,
    observed_ns: int, page_complete: bool, truncated: bool,
) -> HLFilledQuantity:
    """Build an oid-bound fill quantity and evidence from one response."""
    if not isinstance(coin, str):
        raise TypeError("coin must be a string")
    if coin not in COINS:
        raise ValueError("coin must be BTC or ETH")
    if not isinstance(intended_side, str):
        raise TypeError("intended_side must be a string")
    if intended_side not in ORDER_SIDES:
        raise ValueError("intended_side must be buy or sell")
    if not isinstance(oids, frozenset) or any(type(oid) is not int for oid in oids):
        raise TypeError("oids must be a frozenset of integers")
    if any(oid < 0 for oid in oids):
        raise ValueError("oids must be non-negative")
    _nonnegative_int(since_ms, "since_ms")
    _nonnegative_int(skew_allowance_ms, "skew_allowance_ms")
    evidence = parse_fills_surface(
        pages, observed_ns=observed_ns, page_complete=page_complete, truncated=truncated
    )

    states: dict[str, str] = {}
    signed = Decimal(0)
    earliest_ms = max(0, since_ms - skew_allowance_ms)
    for row in (row for page in pages for row in page):
        parsed = _fill_row(row)
        if parsed is None or parsed[1] in states:
            continue
        states[parsed[1]] = parsed[0]
        if row["coin"] == coin and row["oid"] in oids and row["time"] >= earliest_ms:
            signed += Decimal(row["sz"]) * (1 if row["side"] == "B" else -1)
    aligned = signed * (1 if intended_side == "buy" else -1)
    return HLFilledQuantity(aligned if aligned >= 0 else None, evidence)
