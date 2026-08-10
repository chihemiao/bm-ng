import pytest

from reconciliation.ledger import (
    BalanceDelta,
    LedgerContractError,
    reconcile_balance_ledger,
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
