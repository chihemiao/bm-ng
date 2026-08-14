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
    reduce_only: bool
    replacement_ordinal: int
    client_order_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class T0APairIntents:
    hyperliquid: OrderIntent
    bybit: OrderIntent


@dataclass(frozen=True, slots=True, kw_only=True)
class FlattenIntentPlan:
    strategy_id: str
    strategy_version: str
    signal_ns: int
    hyperliquid: OrderIntent | None
    bybit: OrderIntent | None

    def __post_init__(self) -> None:
        if type(self.strategy_id) is not str or not self.strategy_id:
            raise ValueError("strategy_id must be a non-empty str")
        if type(self.strategy_version) is not str or not self.strategy_version:
            raise ValueError("strategy_version must be a non-empty str")
        if type(self.signal_ns) is not int or self.signal_ns < 0:
            raise ValueError("signal_ns must be a non-negative int")
        for leg, intent in (("hyperliquid", self.hyperliquid), ("bybit", self.bybit)):
            if intent is None:
                continue
            if not isinstance(intent, OrderIntent):
                raise TypeError(f"{leg} slot must be an OrderIntent or None")
            if intent.leg != leg:
                raise ValueError(f"{leg} slot holds an intent for leg={intent.leg!r}")
            if intent.reduce_only is not True:
                raise ValueError(f"{leg} intent must have reduce_only=True")
            metadata = (intent.strategy_id, intent.strategy_version, intent.signal_ns)
            if metadata != (self.strategy_id, self.strategy_version, self.signal_ns):
                raise ValueError(f"{leg} intent metadata diverges from plan-level metadata")


def next_flatten_intent(plan: FlattenIntentPlan) -> OrderIntent | None:
    """Choose one intent that minimizes residual exposure if it alone fills."""
    if not isinstance(plan, FlattenIntentPlan):
        raise TypeError("plan must be a FlattenIntentPlan")
    hyperliquid, bybit = plan.hyperliquid, plan.bybit
    if hyperliquid is None:
        return bybit
    if bybit is None:
        return hyperliquid
    if hyperliquid.symbol != bybit.symbol:
        raise ValueError("flatten plan symbols differ")
    return hyperliquid if hyperliquid.quantity >= bybit.quantity else bybit


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
    symbol: str, side: str, quantity: Decimal, reduce_only: bool, ordinal: int,
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
        symbol, side, canonical_quantity, reduce_only, ordinal,
    ]
    payload = json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()
    digest = hashlib.blake2s(payload, digest_size=16, person=b"hlcarry").hexdigest()
    return f"0x{digest}"


def rehydrate_order_intent(
    strategy_id: str, strategy_version: str, signal_ns: int, leg: str,
    *, symbol: str, side: str, quantity: Decimal, reduce_only: bool,
    replacement_ordinal: int,
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
    if type(reduce_only) is not bool:
        raise TypeError("reduce_only must be bool")
    _require(_valid_ns(replacement_ordinal), "invalid replacement_ordinal")
    client_order_id = _client_order_id(
        strategy_id, strategy_version, signal_ns, leg,
        symbol, side, quantity, reduce_only, replacement_ordinal,
    )
    return OrderIntent(
        strategy_id, strategy_version, signal_ns, leg,
        symbol, side, quantity, reduce_only, replacement_ordinal, client_order_id,
    )


def make_order_intent(
    strategy_id: str, strategy_version: str, signal_ns: int, leg: str,
    *, symbol: str, side: str, quantity: Decimal, reduce_only: bool,
) -> OrderIntent:
    return rehydrate_order_intent(
        strategy_id, strategy_version, signal_ns, leg,
        symbol=symbol, side=side, quantity=quantity,
        reduce_only=reduce_only, replacement_ordinal=0,
    )


def make_t0a_pair_intents(
    strategy_id: str, strategy_version: str, signal_ns: int,
    *, symbol: str, quantity: Decimal,
) -> T0APairIntents:
    hyperliquid = make_order_intent(
        strategy_id, strategy_version, signal_ns, "hyperliquid",
        symbol=symbol, side="sell", quantity=quantity, reduce_only=False,
    )
    bybit = make_order_intent(
        strategy_id, strategy_version, signal_ns, "bybit",
        symbol=symbol, side="buy", quantity=quantity, reduce_only=False,
    )
    return T0APairIntents(hyperliquid=hyperliquid, bybit=bybit)


def t0a_pair_intents_match(pair: T0APairIntents) -> bool:
    if not isinstance(pair, T0APairIntents):
        raise TypeError("pair must be T0APairIntents")
    hyperliquid, bybit = pair.hyperliquid, pair.bybit
    topology = (
        hyperliquid.leg, hyperliquid.side, hyperliquid.reduce_only,
        bybit.leg, bybit.side, bybit.reduce_only,
    ) == ("hyperliquid", "sell", False, "bybit", "buy", False)
    shared = ("strategy_id", "strategy_version", "signal_ns", "symbol", "quantity")
    return topology and all(
        getattr(hyperliquid, field) == getattr(bybit, field) for field in shared
    )


def _validate_intent(intent: OrderIntent) -> None:
    _require(isinstance(intent, OrderIntent), "invalid intent")
    expected = rehydrate_order_intent(
        intent.strategy_id, intent.strategy_version, intent.signal_ns, intent.leg,
        symbol=intent.symbol, side=intent.side, quantity=intent.quantity,
        reduce_only=intent.reduce_only, replacement_ordinal=intent.replacement_ordinal,
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


def _authoritative_after(
    intent: OrderIntent,
    evidence: ReconciliationEvidence,
    request: OrderRequestRecord | None = None,
) -> bool:
    watermark_ns = intent.signal_ns if request is None else request.recorded_ns
    times = (evidence.orders_ns, evidence.fills_ns, evidence.positions_ns)
    return all(value is not None and value > watermark_ns for value in times)


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
    ambiguous |= evidence.status == "absent" and not _authoritative_after(
        intent, evidence, request,
    )
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
        reduce_only=previous.reduce_only,
        replacement_ordinal=previous.replacement_ordinal + 1,
    )
