"""Pure, fail-closed order identity and submission contracts."""

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from data.schema_dispatch import ORDER_LEGS, ORDER_SIDES, ORDER_STATUSES, ORDER_SYMBOLS
from data.schema_order_request import order_request_lease_binding_errors

REPLACEABLE_STATUSES = frozenset({"cancelled", "rejected"})
HOLD_STATUSES = frozenset({"open", "partially_filled", "filled", "cancelled", "rejected"})
INTENT_FIELDS = ("strategy_id", "strategy_version", "signal_ns", "leg", "replacement_ordinal")


class OrderContractError(ValueError):
    """Raised when order evidence cannot support a safe transition."""


@dataclass(frozen=True, slots=True)
class OrderIntent:
    strategy_id: str
    strategy_version: str
    signal_ns: int
    leg: str
    symbol: str
    side: str
    quantity: Decimal
    replacement_ordinal: int
    client_order_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class T0APairIntents:
    hyperliquid: OrderIntent
    bybit: OrderIntent


@dataclass(frozen=True, slots=True)
class OrderRequestRecord:
    strategy_id: str
    strategy_version: str
    signal_ns: int
    leg: str
    replacement_ordinal: int
    client_order_id: str
    recorded_ns: int
    account_digest: str
    lease_epoch: int
    writer_instance_id: str
    wallet_fingerprint: str
    allocated_nonce: int | None

    def intent_fields(self) -> dict[str, str | int]:
        return {field: getattr(self, field) for field in INTENT_FIELDS}


@dataclass(frozen=True, slots=True)
class ReconciliationEvidence:
    status: str
    orders_ns: int | None
    fills_ns: int | None
    positions_ns: int | None


@dataclass(frozen=True, slots=True)
class ReplayedDecisionHistory:
    """Per-intent reconcile/freeze state rebuilt from the durable event stream."""

    client_order_id: str
    reconcile_attempts: int
    frozen: bool


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise OrderContractError(message)


def _valid_ns(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_lease_snapshot(
    account_digest: str, lease_epoch: int, instance_id: str, wallet_fingerprint: str
) -> None:
    for name, value in (
        ("account_digest", account_digest), ("wallet_fingerprint", wallet_fingerprint),
    ):
        valid = isinstance(value, str) and len(value) == 64
        valid &= all(char in "0123456789abcdef" for char in value)
        _require(valid, f"invalid {name}")
    _require(type(lease_epoch) is int and lease_epoch > 0, "invalid lease_epoch")
    _require(isinstance(instance_id, str) and bool(instance_id), "invalid writer_instance_id")


def _validate_request_binding(
    leg: str, account_digest: str, lease_epoch: int, instance_id: str,
    wallet_fingerprint: str, allocated_nonce: object,
) -> None:
    payload = {
        "account_digest": account_digest, "lease_epoch": lease_epoch,
        "writer_instance_id": instance_id, "wallet_fingerprint": wallet_fingerprint,
        "allocated_nonce": allocated_nonce,
    }
    errors = order_request_lease_binding_errors(payload, venue=leg, has_sequence=True)
    _require(not errors, errors[0] if errors else "")


def _client_order_id(
    strategy_id: str, strategy_version: str, signal_ns: int, leg: str,
    symbol: str, side: str, quantity: Decimal, ordinal: int,
) -> str:
    # Venue evidence preserves raw strings; our identity is numeric and context-independent.
    sign, digits, exponent = quantity.as_tuple()
    coefficient = list(digits)
    while coefficient[-1] == 0:
        coefficient.pop()
        exponent += 1
    canonical_quantity = [sign, "".join(map(str, coefficient)), exponent]
    identity = [
        strategy_id, strategy_version, signal_ns, leg,
        symbol, side, canonical_quantity, ordinal,
    ]
    payload = json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()
    digest = hashlib.blake2s(payload, digest_size=16, person=b"hlcarry").hexdigest()
    return f"0x{digest}"


def rehydrate_order_intent(
    strategy_id: str, strategy_version: str, signal_ns: int, leg: str,
    *, symbol: str, side: str, quantity: Decimal, replacement_ordinal: int,
) -> OrderIntent:
    _require(isinstance(strategy_id, str) and bool(strategy_id), "invalid strategy_id")
    valid_version = isinstance(strategy_version, str) and bool(strategy_version)
    _require(valid_version, "invalid strategy_version")
    _require(_valid_ns(signal_ns), "invalid signal_ns")
    _require(leg in ORDER_LEGS, "invalid leg")
    if not isinstance(symbol, str):
        raise TypeError("symbol must be a string")
    _require(symbol in ORDER_SYMBOLS, "invalid symbol")
    if not isinstance(side, str):
        raise TypeError("side must be a string")
    _require(side in ORDER_SIDES, "invalid side")
    if not isinstance(quantity, Decimal):
        raise TypeError("quantity must be Decimal")
    _require(quantity.is_finite() and quantity > 0, "invalid quantity")
    _require(_valid_ns(replacement_ordinal), "invalid replacement_ordinal")
    client_order_id = _client_order_id(
        strategy_id, strategy_version, signal_ns, leg,
        symbol, side, quantity, replacement_ordinal,
    )
    return OrderIntent(
        strategy_id, strategy_version, signal_ns, leg,
        symbol, side, quantity, replacement_ordinal, client_order_id,
    )


def make_order_intent(
    strategy_id: str, strategy_version: str, signal_ns: int, leg: str,
    *, symbol: str, side: str, quantity: Decimal,
) -> OrderIntent:
    return rehydrate_order_intent(
        strategy_id, strategy_version, signal_ns, leg,
        symbol=symbol, side=side, quantity=quantity, replacement_ordinal=0,
    )


def make_t0a_pair_intents(
    strategy_id: str, strategy_version: str, signal_ns: int,
    *, symbol: str, quantity: Decimal,
) -> T0APairIntents:
    hyperliquid = make_order_intent(
        strategy_id, strategy_version, signal_ns, "hyperliquid",
        symbol=symbol, side="sell", quantity=quantity,
    )
    bybit = make_order_intent(
        strategy_id, strategy_version, signal_ns, "bybit",
        symbol=symbol, side="buy", quantity=quantity,
    )
    return T0APairIntents(hyperliquid=hyperliquid, bybit=bybit)


def _validate_intent(intent: OrderIntent) -> None:
    _require(isinstance(intent, OrderIntent), "invalid intent")
    expected = rehydrate_order_intent(
        intent.strategy_id, intent.strategy_version, intent.signal_ns, intent.leg,
        symbol=intent.symbol, side=intent.side, quantity=intent.quantity,
        replacement_ordinal=intent.replacement_ordinal,
    )
    _require(intent.client_order_id == expected.client_order_id, "invalid client_order_id")


def order_request_record(
    intent: OrderIntent, recorded_ns: int, *, account_digest: str,
    lease_epoch: int, writer_instance_id: str, wallet_fingerprint: str,
    allocated_nonce: int | None,
) -> OrderRequestRecord:
    _validate_intent(intent)
    _require(_valid_ns(recorded_ns) and recorded_ns >= intent.signal_ns, "invalid recorded_ns")
    _validate_lease_snapshot(
        account_digest, lease_epoch, writer_instance_id, wallet_fingerprint
    )
    _validate_request_binding(
        intent.leg, account_digest, lease_epoch, writer_instance_id,
        wallet_fingerprint, allocated_nonce,
    )
    values = [getattr(intent, field) for field in INTENT_FIELDS]
    return OrderRequestRecord(
        *values, intent.client_order_id, recorded_ns,
        account_digest, lease_epoch, writer_instance_id, wallet_fingerprint, allocated_nonce,
    )


def _validate_evidence(evidence: ReconciliationEvidence) -> None:
    _require(isinstance(evidence, ReconciliationEvidence), "invalid evidence")
    _require(evidence.status in ORDER_STATUSES, "invalid order status")
    for value in (evidence.orders_ns, evidence.fills_ns, evidence.positions_ns):
        _require(value is None or _valid_ns(value), "invalid evidence timestamp")


def _authoritative_after(intent: OrderIntent, evidence: ReconciliationEvidence) -> bool:
    times = (evidence.orders_ns, evidence.fills_ns, evidence.positions_ns)
    return all(value is not None and value > intent.signal_ns for value in times)


def _validate_history(intent: OrderIntent, history: ReplayedDecisionHistory) -> None:
    _require(isinstance(history, ReplayedDecisionHistory), "invalid history")
    _require(history.client_order_id == intent.client_order_id, "history does not match intent")
    _require(_valid_ns(history.reconcile_attempts), "invalid reconcile_attempts")
    _require(isinstance(history.frozen, bool), "invalid frozen state")


def _validate_request(intent: OrderIntent, request: OrderRequestRecord | None) -> None:
    if request is None:
        return
    _require(isinstance(request, OrderRequestRecord), "invalid request record")
    _validate_lease_snapshot(
        request.account_digest, request.lease_epoch,
        request.writer_instance_id, request.wallet_fingerprint,
    )
    _validate_request_binding(
        request.leg, request.account_digest, request.lease_epoch,
        request.writer_instance_id, request.wallet_fingerprint, request.allocated_nonce,
    )
    intent_fields = {field: getattr(intent, field) for field in INTENT_FIELDS}
    matches = request.client_order_id == intent.client_order_id
    matches &= request.intent_fields() == intent_fields
    _require(matches and request.recorded_ns >= intent.signal_ns, "request does not match intent")


def decide_submission(
    intent: OrderIntent,
    evidence: ReconciliationEvidence,
    request: OrderRequestRecord | None,
    history: ReplayedDecisionHistory,
    now_ns: int,
    max_signal_age_ns: int,
    max_reconcile_attempts: int,
) -> str:
    _validate_intent(intent)
    _validate_evidence(evidence)
    _validate_request(intent, request)
    _validate_history(intent, history)
    _require(_valid_ns(now_ns) and now_ns >= intent.signal_ns, "clock moved backwards")
    _require(
        isinstance(max_signal_age_ns, int) and max_signal_age_ns > 0,
        "invalid max_signal_age_ns",
    )
    _require(
        isinstance(max_reconcile_attempts, int) and max_reconcile_attempts > 0,
        "invalid max_reconcile_attempts",
    )
    if history.frozen:
        return "freeze"
    ambiguous = evidence.status in {"pending", "unknown"}
    ambiguous |= evidence.status == "absent" and not _authoritative_after(intent, evidence)
    if ambiguous:
        return "freeze" if history.reconcile_attempts >= max_reconcile_attempts else "reconcile"
    if evidence.status in HOLD_STATUSES:
        return "hold"
    if now_ns - intent.signal_ns > max_signal_age_ns:
        return "reject_stale"
    return "persist" if request is None else "submit"


def replacement_intent(
    previous: OrderIntent, evidence: ReconciliationEvidence, *, quantity: Decimal,
) -> OrderIntent:
    _validate_intent(previous)
    _validate_evidence(evidence)
    _require(evidence.status in REPLACEABLE_STATUSES, "order is not replaceable")
    _require(
        _authoritative_after(previous, evidence),
        "authoritative terminal evidence required",
    )
    return rehydrate_order_intent(
        previous.strategy_id, previous.strategy_version, previous.signal_ns, previous.leg,
        symbol=previous.symbol, side=previous.side, quantity=quantity,
        replacement_ordinal=previous.replacement_ordinal + 1,
    )
