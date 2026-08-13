"""Normalize documented Hyperliquid position snapshots."""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from reconciliation.exposure import LegPosition
from reconciliation.hl_common import COINS, _canonical_rows, _fingerprint, _valid_observed_ns
from reconciliation.state import CanonicalSet, SurfaceEvidence

IDENTITY_SCHEME = "hyperliquid.positions.identity"
STATE_SCHEME = "hyperliquid.positions.state"
POSITION_FIELDS = frozenset(
    {
        "coin", "cumFunding", "entryPx", "leverage", "liquidationPx", "marginUsed",
        "maxLeverage", "positionValue", "returnOnEquity", "szi", "unrealizedPnl",
    }
)


def _position_value(row: object) -> Mapping[str, object] | None:
    # Non-empty row shape is sourced from the official example, not a live non-empty account.
    if not isinstance(row, Mapping) or set(row) != {"position", "type"}:
        return None
    position = row["position"]
    if row["type"] != "oneWay" or not isinstance(position, Mapping):
        return None
    if set(position) != POSITION_FIELDS or position.get("coin") not in COINS:
        return None
    coin = position["coin"]
    szi = position["szi"]
    if not isinstance(coin, str) or not isinstance(szi, str):
        return None
    try:
        if not szi or not Decimal(szi).is_finite():
            return None
    except InvalidOperation:
        return None
    return position


def _position_row(row: object) -> tuple[str, str] | None:
    position = _position_value(row)
    if position is None:
        return None
    try:
        return _fingerprint(row), _fingerprint({"coin": position["coin"]})
    except (TypeError, ValueError):
        return None


def parse_positions_surface(
    payload: Mapping[str, object], *, observed_ns: int
) -> SurfaceEvidence:
    """Parse one current positions snapshot without doing any I/O."""
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    _valid_observed_ns(observed_ns)
    rows = payload.get("assetPositions")
    if not isinstance(rows, list):
        raise ValueError("assetPositions must be a list")

    states, unknown, mismatch = _canonical_rows(rows, _position_row)

    # HL documents no pagination for this snapshot; this is an accepted assumption,
    # not proof that the endpoint can never truncate.
    return SurfaceEvidence(
        observed_ns=observed_ns,
        fetched_count=len(rows),
        page_complete=True,
        truncated=False,
        unknown_count=unknown,
        mismatch_count=mismatch,
        entities=CanonicalSet(STATE_SCHEME, 1, frozenset(states.values())),
        identities=CanonicalSet(IDENTITY_SCHEME, 1, frozenset(states)),
    )


def build_hl_leg_position(
    payload: Mapping[str, object], *, symbol: str, observed_ns: int
) -> LegPosition:
    """Build one HL position value and its evidence from the same snapshot."""
    if not isinstance(symbol, str):
        raise TypeError("symbol must be a string")
    if symbol not in COINS:
        raise ValueError("symbol must be BTC or ETH")
    evidence = parse_positions_surface(payload, observed_ns=observed_ns)
    quantity = Decimal(0)
    for row in payload["assetPositions"]:
        position = _position_value(row)
        if position is not None and position["coin"] == symbol:
            quantity = Decimal(position["szi"])
            break
    return LegPosition("hyperliquid", symbol, quantity, evidence)
