import inspect
from dataclasses import replace
from typing import get_type_hints

import pytest

from reconciliation.ledger import BalanceLedger
from reconciliation.state import (
    CanonicalSet,
    ExpectedSurface,
    StartupContractError,
    SurfaceEvidence,
    VenueEvidence,
    VenueExpectation,
    classify_reconciliation_consistency,
)

LEDGER = BalanceLedger(0, 100, (("USDC", "1"),), (("USDC", "1"),), (), frozenset(), True)


def _surface(name):
    return SurfaceEvidence(
        100, 1, True, False, 0, 0,
        CanonicalSet(f"{name}.state", 1, frozenset({f"{name}-state"})),
        CanonicalSet(f"{name}.identity", 1, frozenset({f"{name}-identity"})),
    )


def _pair():
    venue = VenueEvidence(**{name: _surface(name) for name in (
        "orders", "fills", "positions", "balances")})
    expected = {
        name: ExpectedSurface(getattr(venue, name).entities, getattr(venue, name).identities)
        for name in ("orders", "fills", "positions", "balances")
    }
    expectation = VenueExpectation(
        **expected, frozen_intents=frozenset(), balance_ledger=LEDGER)
    return venue, expectation


def _inputs():
    venue, expectation = _pair()
    return {
        "now_ns": 100, "max_age_ns": 0,
        "venues": {"hyperliquid": venue, "bybit": venue},
        "expectations": {"hyperliquid": expectation, "bybit": expectation},
    }


def _classify(**changes):
    values = _inputs()
    values.update(changes)
    return classify_reconciliation_consistency(**values)


def test_authoritative_matching_state_is_consistent_at_zero_age_boundary():
    assert _classify() is True


@pytest.mark.parametrize("venue", ["hyperliquid", "bybit"])
@pytest.mark.parametrize("surface", ["orders", "fills", "positions", "balances"])
@pytest.mark.parametrize(
    "changes",
    [
        {"observed_ns": 99}, {"page_complete": False}, {"truncated": True},
        {"fetched_count": 2, "unknown_count": 1}, {"mismatch_count": 1},
    ],
)
def test_any_non_authoritative_surface_is_unclassified(venue, surface, changes):
    values = _inputs()
    evidence = values["venues"][venue]
    values["venues"][venue] = replace(
        evidence, **{surface: replace(getattr(evidence, surface), **changes)})
    assert classify_reconciliation_consistency(**values) is None


@pytest.mark.parametrize("ledger", [None, "unknown", "inconsistent"])
def test_untrusted_balance_ledger_is_unclassified(ledger):
    values = _inputs()
    expectation = values["expectations"]["bybit"]
    if ledger == "unknown":
        ledger = replace(LEDGER, unknown_entry_ids=frozenset({"entry"}))
    elif ledger == "inconsistent":
        ledger = replace(LEDGER, snapshot_balances=(("USDC", "2"),), self_consistent=False)
    values["expectations"]["bybit"] = replace(expectation, balance_ledger=ledger)
    assert classify_reconciliation_consistency(**values) is None


@pytest.mark.parametrize("mismatch", ["orders", "fills", "ledger_coverage"])
def test_authoritative_confirmed_mismatch_is_inconsistent(mismatch):
    values = _inputs()
    expectation = values["expectations"]["hyperliquid"]
    if mismatch == "ledger_coverage":
        changed = replace(expectation, balance_ledger=replace(LEDGER, end_ns=99))
    else:
        expected = getattr(expectation, mismatch)
        extra = frozenset({*expected.entities.fingerprints, "missing-state"})
        identities = frozenset({*expected.identities.fingerprints, "missing-identity"})
        changed = replace(
            expectation,
            **{mismatch: replace(
                expected, entities=replace(expected.entities, fingerprints=extra),
                identities=replace(expected.identities, fingerprints=identities),
            )},
        )
    values["expectations"]["hyperliquid"] = changed
    assert classify_reconciliation_consistency(**values) is False


def test_frozen_intent_does_not_change_state_classification():
    values = _inputs()
    expectation = values["expectations"]["bybit"]
    values["expectations"]["bybit"] = replace(
        expectation, frozen_intents=frozenset({"frozen"}))
    assert classify_reconciliation_consistency(**values) is True


@pytest.mark.parametrize("age,error", [(True, StartupContractError), (-1, StartupContractError)])
def test_max_age_must_be_an_exact_nonnegative_integer(age, error):
    with pytest.raises(error, match="max_age_ns"):
        _classify(max_age_ns=age)


def test_invalid_structure_raises_instead_of_becoming_unknown():
    values = _inputs()
    values["venues"].pop("bybit")
    with pytest.raises(StartupContractError, match="venues"):
        classify_reconciliation_consistency(**values)


def test_classifier_has_only_evidence_inputs_and_optional_result():
    function = classify_reconciliation_consistency
    assert tuple(inspect.signature(function).parameters) == (
        "now_ns", "max_age_ns", "venues", "expectations")
    assert get_type_hints(function)["return"] == bool | None
    assert "does not produce a streak input" in inspect.getdoc(function)
