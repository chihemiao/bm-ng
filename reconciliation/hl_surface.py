"""Normalize documented Hyperliquid account snapshots into reconciliation evidence."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation

from reconciliation.exposure import LegPosition
from reconciliation.state import CanonicalSet, SurfaceEvidence, canonical_fingerprint

_fingerprint = canonical_fingerprint

COINS = frozenset({"BTC", "ETH"})
IDENTITY_SCHEME = "hyperliquid.positions.identity"
STATE_SCHEME = "hyperliquid.positions.state"
ORDER_FIELDS = frozenset({"coin", "limitPx", "oid", "side", "sz", "timestamp"})
BALANCE_FIELDS = frozenset({"coin", "entryNtl", "hold", "token", "total"})
BALANCE_TOP_FIELDS = frozenset({"balances", "tokenToAvailableAfterMaintenance"})
FILL_REQUIRED_FIELDS = frozenset(
    {
        "closedPnl", "coin", "crossed", "dir", "fee", "feeToken", "hash", "oid",
        "px", "side", "startPosition", "sz", "tid", "time",
    }
)
FILL_OPTIONAL_FIELDS = frozenset({"builderFee", "liquidation"})
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


def _balance_row(row: object) -> tuple[bool, tuple[str, str] | None]:
    identifiable = isinstance(row, Mapping) and {"coin", "token"} <= set(row)
    if identifiable and row["coin"] != "USDC" and row["token"] != 0:
        return False, None
    valid = (
        isinstance(row, Mapping)
        and set(row) == BALANCE_FIELDS
        and row["coin"] == "USDC"
        and type(row["token"]) is int
        and row["token"] == 0
    )
    try:
        parsed = None if not valid else (_fingerprint(row), _fingerprint({"token": 0}))
    except (TypeError, ValueError):
        parsed = None
    return True, parsed


def _balance_input(spot_payload: object, mode: object, observed_ns: object) -> list[object]:
    if not isinstance(spot_payload, Mapping):
        raise TypeError("spot_payload must be a mapping")
    if set(spot_payload) != BALANCE_TOP_FIELDS:
        raise ValueError("balance payload fields differ")
    rows = spot_payload["balances"]
    if not isinstance(rows, list):
        raise ValueError("balances must be a list")
    _valid_observed_ns(observed_ns)
    if not isinstance(mode, str):
        raise TypeError("mode must be a string")
    if not mode:
        raise ValueError("mode must not be empty")
    return rows


def _fill_row(row: object) -> tuple[str, str] | None:
    if not isinstance(row, Mapping):
        return None
    fields = set(row)
    valid_fields = FILL_REQUIRED_FIELDS <= fields <= FILL_REQUIRED_FIELDS | FILL_OPTIONAL_FIELDS
    tid, time = row.get("tid"), row.get("time")
    valid_identity = row.get("coin") in COINS and all(type(value) is int for value in (tid, time))
    if not valid_fields or not valid_identity or tid < 0 or time < 0:
        return None
    identity = {"time": time, "coin": row["coin"], "tid": tid}
    try:
        return _fingerprint(row), _fingerprint(identity)
    except (TypeError, ValueError):
        return None


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


def parse_balances_surface(
    spot_payload: Mapping[str, object], *, mode: str, observed_ns: int
) -> SurfaceEvidence:
    """Parse the Unified-account USDC collateral balance without doing I/O."""
    rows = _balance_input(spot_payload, mode, observed_ns)
    states: dict[str, str] = {}
    unknown = mismatch = fetched = 0
    found_usdc = False
    if mode == "unifiedAccount":
        for row in rows:
            in_scope, parsed = _balance_row(row)
            if not in_scope:
                continue
            fetched += 1
            found_usdc = True
            if parsed is None:
                unknown += 1
                continue
            state, identity = parsed
            if identity in states:
                unknown += 1
                mismatch += 1
                continue
            states[identity] = state
    # Scope is the validator-operated USDC collateral balance, derived 2026-08-13:
    # hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes
    # hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications
    page_complete = mode == "unifiedAccount" and found_usdc
    return SurfaceEvidence(
        observed_ns=observed_ns,
        fetched_count=fetched,
        page_complete=page_complete,
        truncated=False,
        unknown_count=unknown,
        mismatch_count=mismatch,
        entities=CanonicalSet("hyperliquid.balances.state", 1, frozenset(states.values())),
        identities=CanonicalSet("hyperliquid.balances.identity", 1, frozenset(states)),
    )


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
