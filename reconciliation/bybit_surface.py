"""Normalize documented Bybit position responses into reconciliation evidence."""

from collections.abc import Mapping

from data.schema_dispatch import BYBIT_WIRE_SYMBOLS
from reconciliation.state import CanonicalSet, SurfaceEvidence

RESPONSE_FIELDS = frozenset({"retCode", "retMsg", "result", "retExtInfo", "time"})
RESULT_FIELDS = frozenset({"category", "nextPageCursor", "list"})
IDENTITY_SCHEME = "bybit.positions.identity"
STATE_SCHEME = "bybit.positions.state"


def _validate_inputs(payload: object, symbol: object, observed_ns: object) -> Mapping:
    if not isinstance(symbol, str):
        raise TypeError("symbol must be a string")
    if symbol not in BYBIT_WIRE_SYMBOLS:
        raise ValueError("symbol must be BTC or ETH")
    if type(observed_ns) is not int:
        raise TypeError("observed_ns must be an integer")
    if observed_ns <= 0:
        raise ValueError("observed_ns must be positive")
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    return payload


def _result(payload: Mapping) -> Mapping:
    # The common response fields are documented as complete; position row fields are not.
    if set(payload) != RESPONSE_FIELDS:
        raise ValueError("response fields differ")
    if type(payload["retCode"]) is not int:
        raise TypeError("retCode must be an integer")
    if payload["retCode"] != 0:
        raise ValueError("Bybit response failed")
    if not isinstance(payload["retMsg"], str):
        raise TypeError("retMsg must be a string")
    if not isinstance(payload["retExtInfo"], Mapping):
        raise TypeError("retExtInfo must be a mapping")
    if type(payload["time"]) is not int:
        raise TypeError("time must be an integer")
    if payload["time"] < 0:
        raise ValueError("time must be non-negative")
    result = payload["result"]
    if not isinstance(result, Mapping):
        raise TypeError("result must be a mapping")
    if set(result) != RESULT_FIELDS:
        raise ValueError("result fields differ")
    return result


def parse_bybit_positions_surface(
    payload: Mapping[str, object], *, symbol: str, observed_ns: int
) -> SurfaceEvidence:
    """Parse one successful per-symbol response without doing any I/O."""
    value = _validate_inputs(payload, symbol, observed_ns)
    result = _result(value)
    if result["category"] != "linear":
        raise ValueError("category must be linear")
    cursor, rows = result["nextPageCursor"], result["list"]
    if not isinstance(cursor, str):
        raise TypeError("nextPageCursor must be a string")
    if type(rows) is not list:
        raise TypeError("list must be a list")
    empty = frozenset()
    truncated = bool(cursor)
    return SurfaceEvidence(
        observed_ns=observed_ns,
        fetched_count=len(rows),
        page_complete=bool(rows) and not truncated,
        truncated=truncated,
        unknown_count=len(rows),
        mismatch_count=0,
        entities=CanonicalSet(STATE_SCHEME, 1, empty),
        identities=CanonicalSet(IDENTITY_SCHEME, 1, empty),
    )
