from decimal import Decimal

import pytest

from reconciliation.fx import FxRate, Notional
from reconciliation.ledger import (
    T0A_COLLATERAL_ASSETS,
    BalanceDelta,
    BalanceLedger,
    LedgerContractError,
    reconcile_balance_ledger,
    total_collateral_usdc,
    validate_balance_ledger,
)


def _delta(
    entry_id: str, amount: str, *, asset: str = "USDC", occurred_ns: int = 150
) -> BalanceDelta:
    return BalanceDelta(entry_id, occurred_ns, asset, amount)


def test_fill_and_non_order_deltas_fold_with_exact_decimal_arithmetic() -> None:
    audit = reconcile_balance_ledger(
        start_ns=100,
        end_ns=200,
        opening_balances={"USDC": "1", "USDT": "0"},
        fill_deltas=[_delta("fill-1", "0.1")],
        account_entries=[_delta("funding-1", "0.2"), _delta("fee-1", "-0.05")],
        closing_balances={"USDC": "1.25", "USDT": "0"},
    )

    assert audit.folded_balances == (("USDC", "1.25"), ("USDT", "0"))
    assert audit.snapshot_balances == audit.folded_balances
    assert audit.applied_entry_ids == ("fee-1", "fill-1", "funding-1")
    assert audit.unknown_entry_ids == frozenset()
    assert audit.self_consistent is True


def test_snapshot_mismatch_and_unknown_entries_remain_explicit() -> None:
    audit = reconcile_balance_ledger(
        start_ns=100,
        end_ns=200,
        opening_balances={"USDC": "1"},
        fill_deltas=[],
        account_entries=[_delta("funding-1", "0.2")],
        closing_balances={"USDC": "1.19"},
        unknown_entry_ids=frozenset({"venue-row-9"}),
    )

    assert audit.folded_balances == (("USDC", "1.2"),)
    assert audit.self_consistent is False
    assert audit.unknown_entry_ids == frozenset({"venue-row-9"})


def test_a_forged_consistency_flag_is_rejected() -> None:
    audit = BalanceLedger(100, 200, (("USDC", "1"),), (("USDC", "2"),), (), frozenset(), True)

    with pytest.raises(LedgerContractError, match="consistency"):
        validate_balance_ledger(audit)


def test_duplicate_or_out_of_window_entries_are_rejected() -> None:
    values = {
        "start_ns": 100,
        "end_ns": 200,
        "opening_balances": {"USDC": "1"},
        "closing_balances": {"USDC": "1"},
    }
    with pytest.raises(LedgerContractError, match="duplicate"):
        reconcile_balance_ledger(
            **values,
            fill_deltas=[_delta("same", "0")],
            account_entries=[_delta("same", "0")],
        )
    with pytest.raises(LedgerContractError, match="window"):
        reconcile_balance_ledger(
            **values,
            fill_deltas=[_delta("late", "0", occurred_ns=201)],
            account_entries=[],
        )


@pytest.mark.parametrize("amount", ["1.00", "01", "-0", "1e-2", "NaN"])
def test_noncanonical_decimal_amounts_are_rejected(amount: str) -> None:
    with pytest.raises(LedgerContractError, match="canonical amount"):
        _delta("bad", amount)


def test_opening_and_snapshot_asset_identities_must_match() -> None:
    with pytest.raises(LedgerContractError, match="asset identities"):
        reconcile_balance_ledger(
            start_ns=100,
            end_ns=200,
            opening_balances={"USDC": "1"},
            fill_deltas=[],
            account_entries=[],
            closing_balances={"USDT": "1"},
        )


def _collateral_ledger(
    asset: str,
    amount: str,
    *,
    consistent: bool = True,
    unknown: frozenset[str] = frozenset(),
    extra: tuple[tuple[str, str], ...] = (),
) -> BalanceLedger:
    folded = tuple(sorted(((asset, amount), *extra)))
    snapshot_amount = amount if consistent else "999"
    snapshot = tuple(sorted(((asset, snapshot_amount), *extra)))
    return BalanceLedger(100, 200, folded, snapshot, (), unknown, consistent)


def _venue_ledger(venue: str, amount: str | None = None, **changes) -> BalanceLedger:
    assets = dict(T0A_COLLATERAL_ASSETS)
    value = amount or ("10.25" if venue == "hyperliquid" else "5")
    return _collateral_ledger(assets[venue], value, **changes)


def _total(**changes):
    values = {
        "hyperliquid": _venue_ledger("hyperliquid"),
        "bybit": _venue_ledger("bybit"),
        "rate": FxRate("USDT", "USDC", Decimal("1.001"), 100),
        "now_ns": 110,
        "max_age_ns": 10,
    }
    values.update(changes)
    return total_collateral_usdc(**values)


def test_t0a_collateral_assets_are_bound_to_the_frozen_venues() -> None:
    assert T0A_COLLATERAL_ASSETS == (
        ("hyperliquid", "USDC"),
        ("bybit", "USDT"),
    )


def test_total_collateral_converts_bybit_and_binds_the_usdc_unit() -> None:
    assert _total() == Notional(Decimal("15.255"), "USDC")


@pytest.mark.parametrize("venue", ["hyperliquid", "bybit"])
def test_inconsistent_venue_ledger_makes_total_unknown(venue: str) -> None:
    assert _total(**{venue: _venue_ledger(venue, consistent=False)}) is None


@pytest.mark.parametrize("venue", ["hyperliquid", "bybit"])
def test_unknown_venue_entry_makes_total_unknown(venue: str) -> None:
    ledger = _venue_ledger(venue, unknown=frozenset({"unclassified-row"}))
    assert _total(**{venue: ledger}) is None


@pytest.mark.parametrize(
    ("venue", "unexpected_asset"),
    [("hyperliquid", "USDT"), ("bybit", "USDC")],
)
def test_missing_expected_collateral_asset_is_unknown_not_zero(
    venue: str, unexpected_asset: str
) -> None:
    missing = _collateral_ledger(unexpected_asset, "10")
    assert _total(**{venue: missing}) is None


@pytest.mark.parametrize(
    "rate",
    [None, FxRate("USDT", "USDC", Decimal("1.001"), 99)],
)
def test_missing_or_stale_fx_keeps_total_unknown(rate: FxRate | None) -> None:
    assert _total(rate=rate) is None


def test_extra_assets_are_ignored_conservatively() -> None:
    extra = (("BTC", "999999"),)
    assert _total(
        hyperliquid=_venue_ledger("hyperliquid", extra=extra),
        bybit=_venue_ledger("bybit", extra=extra),
    ) == _total()


@pytest.mark.parametrize(
    ("hyperliquid_amount", "bybit_amount"),
    [("-1", "0.5"), ("10", "-1")],
)
def test_negative_component_or_total_is_known_invalid(
    hyperliquid_amount: str, bybit_amount: str
) -> None:
    with pytest.raises(ValueError, match="negative collateral"):
        _total(
            hyperliquid=_venue_ledger("hyperliquid", hyperliquid_amount),
            bybit=_venue_ledger("bybit", bybit_amount),
        )


@pytest.mark.parametrize("venue", ["hyperliquid", "bybit"])
def test_total_requires_real_balance_ledgers(venue: str) -> None:
    with pytest.raises(TypeError, match=venue):
        _total(**{venue: object()})


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("now_ns", True, TypeError),
        ("now_ns", 0, ValueError),
        ("max_age_ns", 1.0, TypeError),
        ("max_age_ns", 0, ValueError),
    ],
)
def test_total_collateral_clock_inputs_are_strict(
    field: str, value: object, error: type[Exception]
) -> None:
    with pytest.raises(error, match=field):
        _total(**{field: value})


def test_invalid_ledger_contract_is_not_downgraded_to_unknown() -> None:
    forged = BalanceLedger(
        100, 200, (("USDC", "1"),), (("USDC", "2"),), (), frozenset(), True
    )
    with pytest.raises(LedgerContractError, match="consistency"):
        _total(hyperliquid=forged)


def test_total_collateral_keeps_all_decimal_precision() -> None:
    rate = FxRate("USDT", "USDC", Decimal("0.9987654321"), 100)
    total = _total(
        hyperliquid=_venue_ledger("hyperliquid", "0.123456789"),
        bybit=_venue_ledger("bybit", "0.987654321"),
        rate=rate,
    )
    assert total == Notional(Decimal("1.1098917836789971041"), "USDC")
