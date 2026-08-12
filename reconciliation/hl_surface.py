"""Normalize documented Hyperliquid account snapshots into reconciliation evidence."""

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping

from reconciliation.state import CanonicalSet, SurfaceEvidence

COINS = frozenset({"BTC", "ETH"})
IDENTITY_SCHEME = "hyperliquid.positions.identity"
STATE_SCHEME = "hyperliquid.positions.state"
ORDER_FIELDS = frozenset({"coin", "limitPx", "oid", "side", "sz", "timestamp"})
BALANCE_FIELDS = frozenset({"coin", "entryNtl", "hold", "token", "total"})
BALANCE_TOP_FIELDS = frozenset({"balances", "tokenToAvailableAfterMaintenance"})
POSITION_FIELDS = frozenset(
    {
        "coin", "cumFunding", "entryPx", "leverage", "liquidationPx", "marginUsed",
        "maxLeverage", "positionValue", "returnOnEquity", "szi", "unrealizedPnl",
    }
)


def _fingerprint(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _position_row(row: object) -> tuple[str, str] | None:
    # Non-empty row shape is sourced from the official example, not a live non-empty account.
    if not isinstance(row, Mapping) or set(row) != {"position", "type"}:
        return None
    position = row["position"]
    if row["type"] != "oneWay" or not isinstance(position, Mapping):
        return None
    if set(position) != POSITION_FIELDS or position.get("coin") not in COINS:
        return None
    coin = position["coin"]
    if not isinstance(coin, str):
        return None
    try:
        return _fingerprint(row), _fingerprint({"coin": coin})
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
