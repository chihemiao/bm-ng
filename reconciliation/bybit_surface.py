"""Normalize documented Bybit REST responses into reconciliation evidence."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from data.schema_dispatch import BYBIT_WIRE_SYMBOLS, ORDER_SIDES
from reconciliation.exposure import LegPosition
from reconciliation.state import CanonicalSet, SurfaceEvidence, canonical_fingerprint

_fingerprint = canonical_fingerprint
ROW_FIELDS = frozenset({"positionIdx", "symbol", "side", "size"})

RESPONSE_FIELDS = frozenset({"retCode", "retMsg", "result", "retExtInfo", "time"})
RESULT_FIELDS = frozenset({"category", "nextPageCursor", "list"})
IDENTITY_SCHEME = "bybit.positions.identity"
STATE_SCHEME = "bybit.positions.state"
FILL_FIELDS = frozenset(
    {"symbol", "orderLinkId", "side", "execId", "execQty", "execType", "execTime"})
BYBIT_EXECUTION_TYPES = frozenset(
    {
        "Trade", "AdlTrade", "Funding", "BustTrade", "Delivery", "Settle",
        "BlockTrade", "MovePosition", "FutureSpread", "CorporateAction", "UNKNOWN",
    }
)


@dataclass(frozen=True, slots=True)
class BybitFilledQuantity:
    quantity: Decimal | None
    evidence: SurfaceEvidence


def _validate_inputs(payload: object, symbol: object, observed_ns: object) -> Mapping:
    if not isinstance(symbol, str):
        raise TypeError("symbol must be a string")
    if symbol not in BYBIT_WIRE_SYMBOLS:
        raise ValueError("symbol must be BTC or ETH")
    if type(observed_ns) is not int or observed_ns <= 0:
        error = TypeError if type(observed_ns) is not int else ValueError
        raise error("observed_ns must be a positive integer")
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


def _signed_position_quantity(row: object, symbol: str) -> Decimal | None:
    if not isinstance(row, Mapping) or not ROW_FIELDS <= set(row):
        return None
    if type(row["positionIdx"]) is not int or row["positionIdx"] != 0:
        return None
    if row["symbol"] != BYBIT_WIRE_SYMBOLS[symbol] or not isinstance(row["side"], str):
        return None
    size = row["size"]
    if not isinstance(size, str) or not size:
        return None
    try:
        quantity = Decimal(size)
    except InvalidOperation:
        return None
    if not quantity.is_finite():
        return None
    side = row["side"]
    # Bybit size is unsigned; unlike HL szi, direction belongs only to side.
    if side == "" and quantity == 0:
        return Decimal(0)
    if quantity > 0 and side in {"Buy", "Sell"}:
        return quantity if side == "Buy" else -quantity
    return None


def _position_row(row: object, symbol: str) -> tuple[str, str] | None:
    quantity = _signed_position_quantity(row, symbol)
    if quantity is None:
        return None
    try:
        return _fingerprint(row), _fingerprint({"symbol": symbol})
    except TypeError, ValueError:
        return None


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
    truncated = bool(cursor)
    states: dict[str, str] = {}
    unknown = mismatch = 0
    for row in rows:
        parsed = _position_row(row, symbol)
        if parsed is not None:
            state, identity = parsed
            if identity not in states:
                states[identity] = state
                continue
            mismatch += 1
        unknown += 1
    return SurfaceEvidence(
        observed_ns=observed_ns,
        fetched_count=len(rows),
        page_complete=bool(rows) and not truncated,
        truncated=truncated,
        unknown_count=unknown,
        mismatch_count=mismatch,
        entities=CanonicalSet(STATE_SCHEME, 1, frozenset(states.values())),
        identities=CanonicalSet(IDENTITY_SCHEME, 1, frozenset(states)),
    )


def build_bybit_leg_position(
    payload: Mapping[str, object], *, symbol: str, observed_ns: int
) -> LegPosition:
    """Build one Bybit position value and its evidence from the same snapshot."""
    evidence = parse_bybit_positions_surface(payload, symbol=symbol, observed_ns=observed_ns)
    quantity = Decimal(0)
    for row in payload["result"]["list"]:
        signed_quantity = _signed_position_quantity(row, symbol)
        if signed_quantity is not None:
            quantity = signed_quantity
            break
    return LegPosition("bybit", symbol, quantity, evidence)


def _execution_row(row: object) -> tuple[str, str, Decimal, int] | None:
    if not isinstance(row, Mapping) or not FILL_FIELDS <= set(row):
        return None
    symbol, link, side = row["symbol"], row["orderLinkId"], row["side"]
    exec_id, quantity, exec_time = row["execId"], row["execQty"], row["execTime"]
    valid = row["execType"] == "Trade"
    valid &= symbol in BYBIT_WIRE_SYMBOLS.values() and isinstance(link, str)
    valid &= isinstance(side, str) and side in {"Buy", "Sell"}
    valid &= isinstance(exec_id, str) and bool(exec_id)
    canonical_time = isinstance(exec_time, str) and exec_time.isascii() and exec_time.isdecimal()
    canonical_time = canonical_time and (exec_time == "0" or not exec_time.startswith("0"))
    if not valid or not canonical_time or not isinstance(quantity, str) or not quantity:
        return None
    try:
        parsed_quantity = Decimal(quantity)
    except InvalidOperation:
        return None
    if not parsed_quantity.is_finite() or parsed_quantity < 0:
        return None
    try:
        state = _fingerprint(row)
        identity = _fingerprint({"symbol": symbol, "execId": exec_id})
        return state, identity, parsed_quantity, int(exec_time)
    except (TypeError, ValueError):
        return None


def _fill_results(pages: object, observed_ns: object) -> list[Mapping]:
    if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes)):
        raise TypeError("pages must be a sequence")
    if not pages:
        raise ValueError("pages must not be empty")
    if not all(isinstance(page, Mapping) for page in pages):
        raise TypeError("pages must contain mappings")
    if type(observed_ns) is not int or observed_ns <= 0:
        error = TypeError if type(observed_ns) is not int else ValueError
        raise error("observed_ns must be a positive integer")
    results = [_result(page) for page in pages]
    for result in results:
        if result["category"] != "linear":
            raise ValueError("category must be linear")
        if not isinstance(result["nextPageCursor"], str):
            raise TypeError("nextPageCursor must be a string")
        if type(result["list"]) is not list:
            raise TypeError("list must be a list")
    if any(not result["nextPageCursor"] for result in results[:-1]):
        raise ValueError("non-terminal page has an empty cursor")
    return results


def parse_bybit_fills_surface(
    pages: Sequence[Mapping[str, object]], *, observed_ns: int) -> SurfaceEvidence:
    """Parse REST Trade executions; paginator cursor-chain proof remains external."""
    results = _fill_results(pages, observed_ns)
    states: dict[str, str] = {}
    unknown = mismatch = 0
    for row in (row for result in results for row in result["list"]):
        if isinstance(row, Mapping) and row.get("execType") in BYBIT_EXECUTION_TYPES - {"Trade"}:
            continue
        parsed = _execution_row(row)
        if parsed is None:
            unknown += 1
            continue
        state, identity, _, _ = parsed
        previous = states.get(identity)
        if previous is not None:
            mismatch += previous != state
            continue
        states[identity] = state
    cursor = results[-1]["nextPageCursor"]
    return SurfaceEvidence(
        observed_ns, len(states) + unknown, not cursor, bool(cursor), unknown, mismatch,
        CanonicalSet("bybit.fills.state", 1, frozenset(states.values())),
        CanonicalSet("bybit.fills.identity", 1, frozenset(states)),
    )


def _fill_target(
    order_link_id: object, symbol: object, intended_side: object,
    since_ms: object, skew_allowance_ms: object,
) -> None:
    for name, value in (("order_link_id", order_link_id), ("symbol", symbol),
                        ("intended_side", intended_side)):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
    if not order_link_id:
        raise ValueError("order_link_id must not be empty")
    if symbol not in BYBIT_WIRE_SYMBOLS:
        raise ValueError("symbol must be BTC or ETH")
    if intended_side not in ORDER_SIDES:
        raise ValueError("intended_side must be buy or sell")
    for name, value in (("since_ms", since_ms), ("skew_allowance_ms", skew_allowance_ms)):
        if type(value) is not int:
            raise TypeError(f"{name} must be an integer")
        if value < 0:
            raise ValueError(f"{name} must be non-negative")


def build_bybit_filled_quantity(
    pages: Sequence[Mapping[str, object]], *, order_link_id: str, symbol: str,
    intended_side: str, since_ms: int, skew_allowance_ms: int, observed_ns: int,
) -> BybitFilledQuantity:
    """Build an exact-link, canonical-symbol quantity from one REST response chain."""
    _fill_target(order_link_id, symbol, intended_side, since_ms, skew_allowance_ms)
    evidence = parse_bybit_fills_surface(pages, observed_ns=observed_ns)
    seen = set()
    signed = Decimal(0)
    earliest_ms = max(0, since_ms - skew_allowance_ms)
    for row in (row for page in pages for row in page["result"]["list"]):
        parsed = _execution_row(row)
        if parsed is None or parsed[1] in seen:
            continue
        _, identity, quantity, execution_ms = parsed
        seen.add(identity)
        matches = row["symbol"] == BYBIT_WIRE_SYMBOLS[symbol]
        matches = matches and row["orderLinkId"] == order_link_id
        matches = matches and execution_ms >= earliest_ms
        if matches:
            canonical_side = row["side"].lower()
            signed += quantity if canonical_side == "buy" else -quantity
    aligned = signed if intended_side == "buy" else -signed
    return BybitFilledQuantity(aligned if aligned >= 0 else None, evidence)
