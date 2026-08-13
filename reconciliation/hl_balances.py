"""Normalize documented Hyperliquid collateral-balance snapshots."""

from collections.abc import Mapping

from reconciliation.hl_common import _fingerprint, _valid_observed_ns
from reconciliation.state import CanonicalSet, SurfaceEvidence

BALANCE_FIELDS = frozenset({"coin", "entryNtl", "hold", "token", "total"})
BALANCE_TOP_FIELDS = frozenset({"balances", "tokenToAvailableAfterMaintenance"})


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
