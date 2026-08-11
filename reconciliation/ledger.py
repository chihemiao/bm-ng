"""Exact, venue-bounded balance ledger algebra."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from reconciliation.fx import FxRate, Notional, convert_usdt_to_usdc

T0A_COLLATERAL_ASSETS = (
    ("hyperliquid", "USDC"),
    ("bybit", "USDT"),
)


class LedgerContractError(ValueError):
    """Raised when ledger evidence cannot be folded without guessing."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LedgerContractError(message)


def _amount(text: object) -> Decimal:
    _require(isinstance(text, str) and bool(text), "invalid canonical amount")
    try:
        number = Decimal(text)
    except InvalidOperation as error:
        raise LedgerContractError("invalid canonical amount") from error
    canonical = "0" if number.is_finite() and number == 0 else format(number.normalize(), "f")
    _require(number.is_finite() and canonical == text, "invalid canonical amount")
    return number


def _format_amount(number: Decimal) -> str:
    return "0" if number == 0 else format(number.normalize(), "f")


@dataclass(frozen=True, slots=True)
class BalanceDelta:
    entry_id: str
    occurred_ns: int
    asset: str
    signed_amount_canonical: str

    def __post_init__(self) -> None:
        _require(isinstance(self.entry_id, str) and bool(self.entry_id), "invalid entry_id")
        valid_time = type(self.occurred_ns) is int and self.occurred_ns >= 0
        _require(valid_time, "invalid occurred_ns")
        _require(isinstance(self.asset, str) and bool(self.asset), "invalid asset")
        _amount(self.signed_amount_canonical)


@dataclass(frozen=True, slots=True)
class BalanceLedger:
    start_ns: int
    end_ns: int
    folded_balances: tuple[tuple[str, str], ...]
    snapshot_balances: tuple[tuple[str, str], ...]
    applied_entry_ids: tuple[str, ...]
    unknown_entry_ids: frozenset[str]
    self_consistent: bool


def validate_balance_ledger(ledger: BalanceLedger) -> BalanceLedger:
    _require(isinstance(ledger, BalanceLedger), "invalid balance ledger")
    valid_clock = type(ledger.start_ns) is int and ledger.start_ns >= 0
    valid_clock &= type(ledger.end_ns) is int and ledger.end_ns >= ledger.start_ns
    _require(valid_clock, "invalid ledger window")
    folded = _balance_pairs(ledger.folded_balances)
    snapshot = _balance_pairs(ledger.snapshot_balances)
    ids = ledger.applied_entry_ids
    _require(isinstance(ids, tuple) and ids == tuple(sorted(set(ids))), "invalid entry IDs")
    unknown = ledger.unknown_entry_ids
    valid_unknown = isinstance(unknown, frozenset) and all(
        isinstance(value, str) and bool(value) for value in unknown
    )
    _require(valid_unknown and not unknown.intersection(ids), "invalid unknown entry IDs")
    expected = folded == snapshot
    valid_consistency = type(ledger.self_consistent) is bool
    _require(valid_consistency and ledger.self_consistent is expected, "consistency")
    return ledger


def _balance_pairs(value: object) -> dict[str, Decimal]:
    _require(isinstance(value, tuple) and bool(value), "invalid balance pairs")
    balances = {}
    for pair in value:
        valid_pair = isinstance(pair, tuple) and len(pair) == 2
        _require(valid_pair, "invalid balance pair")
        asset, amount = pair
        _require(isinstance(asset, str) and bool(asset) and asset not in balances, "invalid asset")
        balances[asset] = _amount(amount)
    _require(value == tuple(sorted(value)), "balance pairs are not sorted")
    return balances


def _balances(value: Mapping[str, str]) -> dict[str, Decimal]:
    _require(isinstance(value, Mapping) and bool(value), "invalid balances")
    return _balance_pairs(tuple(sorted(value.items())))


def reconcile_balance_ledger(
    *,
    start_ns: int,
    end_ns: int,
    opening_balances: Mapping[str, str],
    fill_deltas: Iterable[BalanceDelta],
    account_entries: Iterable[BalanceDelta],
    closing_balances: Mapping[str, str],
    unknown_entry_ids: frozenset[str] = frozenset(),
) -> BalanceLedger:
    valid_clock = type(start_ns) is int and start_ns >= 0
    valid_clock &= type(end_ns) is int and end_ns >= start_ns
    _require(valid_clock, "invalid ledger window")
    totals = _balances(opening_balances)
    snapshot = _balances(closing_balances)
    _require(totals.keys() == snapshot.keys(), "balance asset identities differ")
    entry_ids = set()
    for delta in (*tuple(fill_deltas), *tuple(account_entries)):
        _require(isinstance(delta, BalanceDelta), "invalid balance delta")
        _require(start_ns <= delta.occurred_ns <= end_ns, "delta outside ledger window")
        _require(delta.entry_id not in entry_ids, "duplicate ledger entry")
        _require(delta.asset in totals, "delta asset identity is unknown")
        entry_ids.add(delta.entry_id)
        totals[delta.asset] += _amount(delta.signed_amount_canonical)
    folded = tuple(sorted((asset, _format_amount(amount)) for asset, amount in totals.items()))
    observed = tuple(sorted((asset, _format_amount(amount)) for asset, amount in snapshot.items()))
    audit = BalanceLedger(
        start_ns, end_ns, folded, observed, tuple(sorted(entry_ids)), unknown_entry_ids,
        folded == observed,
    )
    return validate_balance_ledger(audit)


def _known_collateral(
    ledger: object, *, venue: str, asset: str
) -> Decimal | None:
    if not isinstance(ledger, BalanceLedger):
        raise TypeError(f"{venue} must be a BalanceLedger")
    validate_balance_ledger(ledger)
    if not ledger.self_consistent or ledger.unknown_entry_ids:
        return None
    return _balance_pairs(ledger.folded_balances).get(asset)


def total_collateral_usdc(
    *,
    hyperliquid: BalanceLedger,
    bybit: BalanceLedger,
    rate: FxRate | None,
    now_ns: int,
    max_age_ns: int,
) -> Notional | None:
    """Aggregate replayable T0A collateral, or None when evidence is unknown."""
    hyperliquid_amount = _known_collateral(
        hyperliquid, venue="hyperliquid", asset=T0A_COLLATERAL_ASSETS[0][1]
    )
    bybit_amount = _known_collateral(
        bybit, venue="bybit", asset=T0A_COLLATERAL_ASSETS[1][1]
    )
    amounts = (hyperliquid_amount, bybit_amount)
    if any(amount is not None and amount < 0 for amount in amounts):
        raise ValueError("negative collateral cannot be represented as Notional")
    converted = convert_usdt_to_usdc(
        Decimal(0) if bybit_amount is None else bybit_amount,
        rate=rate,
        now_ns=now_ns,
        max_age_ns=max_age_ns,
    )
    if hyperliquid_amount is None or bybit_amount is None or converted is None:
        return None
    total = hyperliquid_amount + converted
    if total < 0:
        raise ValueError("negative collateral cannot be represented as Notional")
    return Notional(total, "USDC")
