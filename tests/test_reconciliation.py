from dataclasses import replace

import pytest

from reconciliation.state import (
    AdmissionDecision,
    CanonicalSet,
    ExpectedSurface,
    StartupContractError,
    SurfaceEvidence,
    ValidatedStartup,
    VenueEvidence,
    VenueExpectation,
    decide_startup_admission,
    validate_startup_structure,
    validate_surface_evidence,
)


def _canonical(kind: str, *fingerprints: str) -> CanonicalSet:
    return CanonicalSet(
        scheme_id=f"orders.{kind}",
        scheme_version=1,
        fingerprints=frozenset(fingerprints or {kind}),
    )


def _surface(**changes) -> SurfaceEvidence:
    values = {
        "observed_ns": 150,
        "fetched_count": 1,
        "page_complete": True,
        "truncated": False,
        "unknown_count": 0,
        "mismatch_count": 0,
        "entities": _canonical("state"),
        "identities": _canonical("identity"),
    }
    values.update(changes)
    return SurfaceEvidence(**values)


def _venue() -> VenueEvidence:
    names = ("orders", "fills", "positions", "balances")
    return VenueEvidence(**{name: _surface() for name in names})


def _expectation(**changes) -> VenueExpectation:
    venue = _venue()
    values = {
        name: ExpectedSurface(
            entities=getattr(venue, name).entities,
            identities=getattr(venue, name).identities,
        )
        for name in ("orders", "fills", "positions", "balances")
    }
    values.update(frozen_intents=frozenset(), balance_ledger_available=True)
    values.update(changes)
    return VenueExpectation(**values)


def _venues() -> dict[str, VenueEvidence]:
    return {"hyperliquid": _venue(), "bybit": _venue()}


def _expectations() -> dict[str, VenueExpectation]:
    return {"hyperliquid": _expectation(), "bybit": _expectation()}


def _startup(**changes) -> ValidatedStartup:
    values = {
        "startup_started_ns": 100,
        "now_ns": 200,
        "venues": _venues(),
        "expectations": _expectations(),
    }
    values.update(changes)
    return validate_startup_structure(**values)


def _decision(**changes) -> AdmissionDecision:
    values = {
        "startup_started_ns": 100,
        "now_ns": 200,
        "venues": _venues(),
        "expectations": _expectations(),
    }
    values.update(changes)
    return decide_startup_admission(**values)


def _changed_venue(surface: str, **changes) -> VenueEvidence:
    venue = _venue()
    current = replace(getattr(venue, surface), **changes)
    if "unknown_count" in changes and "fetched_count" not in changes:
        represented = len(current.entities.fingerprints) + current.unknown_count
        current = replace(current, fetched_count=represented)
    return replace(venue, **{surface: current})


def _changed_expected(
    expectation: VenueExpectation, surface: str, **changes
) -> VenueExpectation:
    updated = replace(getattr(expectation, surface), **changes)
    return replace(expectation, **{surface: updated})


def _decision_with_expected(surface: str, **changes) -> AdmissionDecision:
    expectations = _expectations()
    current = expectations["hyperliquid"]
    expectations["hyperliquid"] = _changed_expected(current, surface, **changes)
    return _decision(expectations=expectations)


def test_valid_surface_evidence_is_returned_unchanged() -> None:
    evidence = _surface()

    assert validate_surface_evidence(evidence, now_ns=200) is evidence


def test_unknown_entities_are_included_in_the_fetched_count() -> None:
    evidence = _surface(fetched_count=2, unknown_count=1)

    assert validate_surface_evidence(evidence, now_ns=200) is evidence


@pytest.mark.parametrize(
    "evidence",
    [
        _surface(observed_ns=True),
        _surface(observed_ns=201),
        _surface(fetched_count=-1),
        _surface(unknown_count=-1),
        _surface(mismatch_count=-1),
        _surface(page_complete=1),
        _surface(truncated=0),
    ],
)
def test_invalid_surface_fields_raise(evidence: SurfaceEvidence) -> None:
    with pytest.raises(StartupContractError):
        validate_surface_evidence(evidence, now_ns=200)


def test_fetched_count_must_match_known_and_unknown_entities() -> None:
    with pytest.raises(StartupContractError, match="fetched_count"):
        validate_surface_evidence(_surface(fetched_count=0), now_ns=200)


def test_identity_and_state_cardinality_must_match() -> None:
    identities = replace(_canonical("identity"), fingerprints=frozenset())

    with pytest.raises(StartupContractError, match="cardinality"):
        validate_surface_evidence(_surface(identities=identities), now_ns=200)


def test_identity_and_state_require_distinct_canonicalization_schemes() -> None:
    entities = _canonical("identity")

    with pytest.raises(StartupContractError, match="distinct"):
        validate_surface_evidence(_surface(entities=entities), now_ns=200)


@pytest.mark.parametrize(
    "canonical",
    [
        CanonicalSet("", 1, frozenset({"value"})),
        CanonicalSet("orders.state", 0, frozenset({"value"})),
        CanonicalSet("orders.state", 1, frozenset({""})),
    ],
)
def test_canonical_sets_are_structurally_validated(canonical: CanonicalSet) -> None:
    with pytest.raises(StartupContractError):
        validate_surface_evidence(_surface(entities=canonical), now_ns=200)


@pytest.mark.parametrize("now_ns", [True, -1])
def test_validation_time_must_be_a_nonnegative_integer(now_ns: int) -> None:
    with pytest.raises(StartupContractError, match="now_ns"):
        validate_surface_evidence(_surface(), now_ns=now_ns)


def test_structure_validation_returns_deterministic_data_without_an_action() -> None:
    result = _startup(
        venues={"bybit": _venue(), "hyperliquid": _venue()},
        expectations={"bybit": _expectation(), "hyperliquid": _expectation()},
    )

    assert isinstance(result, ValidatedStartup)
    assert tuple(name for name, _ in result.venues) == ("bybit", "hyperliquid")
    assert not hasattr(result, "action")


@pytest.mark.parametrize("target", ["venues", "expectations"])
def test_structure_requires_exactly_both_venues(target: str) -> None:
    with pytest.raises(StartupContractError, match="venues"):
        _startup(**{target: {"hyperliquid": _venue()}})


def test_venue_and_expectation_keys_must_match() -> None:
    expectations = {"hyperliquid": _expectation(), "other": _expectation()}

    with pytest.raises(StartupContractError, match="venues"):
        _startup(expectations=expectations)


@pytest.mark.parametrize(
    ("startup_started_ns", "now_ns"),
    [(100, 99), (True, 200), (100, True)],
)
def test_startup_clock_must_be_monotonic(startup_started_ns: int, now_ns: int) -> None:
    with pytest.raises(StartupContractError, match="clock"):
        _startup(startup_started_ns=startup_started_ns, now_ns=now_ns)


def test_local_and_venue_canonicalization_contracts_must_match() -> None:
    expected = _expectation()
    entities = replace(expected.orders.entities, scheme_version=2)
    changed = replace(expected, orders=replace(expected.orders, entities=entities))

    with pytest.raises(StartupContractError, match="canonicalization"):
        _startup(expectations={"hyperliquid": changed, "bybit": _expectation()})


@pytest.mark.parametrize(
    "expectation",
    [
        _expectation(frozen_intents={"not-frozen"}),
        _expectation(balance_ledger_available=1),
    ],
)
def test_expectation_control_fields_are_structurally_validated(
    expectation: VenueExpectation,
) -> None:
    with pytest.raises(StartupContractError):
        _startup(expectations={"hyperliquid": expectation, "bybit": _expectation()})


def test_only_complete_fresh_evidence_is_ready() -> None:
    decision = _decision()

    assert decision == AdmissionDecision(action="ready", reasons=())
    assert decision.action in {"ready", "cancel_only_freeze"}
    assert "submit" not in decision.action
    assert "reduce" not in decision.action


@pytest.mark.parametrize("surface", ["orders", "fills", "positions", "balances"])
def test_each_stale_surface_freezes(surface: str) -> None:
    venues = _venues()
    venues["bybit"] = _changed_venue(surface, observed_ns=100)

    decision = _decision(venues=venues)

    assert decision.action == "cancel_only_freeze"
    assert f"bybit.{surface}:stale" in decision.reasons


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"page_complete": False}, "pagination_incomplete"),
        ({"truncated": True}, "truncated"),
        ({"unknown_count": 1}, "unknown_entities"),
        ({"mismatch_count": 1}, "mismatch"),
    ],
)
def test_each_incomplete_surface_condition_freezes(changes: dict, reason: str) -> None:
    venues = _venues()
    venues["hyperliquid"] = _changed_venue("fills", **changes)

    decision = _decision(venues=venues)

    assert decision.action == "cancel_only_freeze"
    assert f"hyperliquid.fills:{reason}" in decision.reasons


def test_reasons_are_sorted_and_deterministic() -> None:
    venues = {
        "hyperliquid": _changed_venue("orders", truncated=True),
        "bybit": _changed_venue("fills", page_complete=False),
    }

    decision = _decision(venues=venues)

    assert decision.reasons == tuple(sorted(decision.reasons))


def test_previous_freeze_is_absorbing() -> None:
    previous = AdmissionDecision("cancel_only_freeze", ("earlier",))

    assert _decision(previous_freeze=previous).reasons == ("startup:previous_freeze",)


def test_replayed_frozen_intent_blocks_startup() -> None:
    expectations = _expectations()
    expectations["hyperliquid"] = _expectation(frozen_intents=frozenset({"0xfrozen"}))

    assert "hyperliquid:frozen_intent" in _decision(expectations=expectations).reasons


def test_missing_balance_ledger_capability_blocks_startup() -> None:
    expectations = _expectations()
    expectations["hyperliquid"] = _expectation(balance_ledger_available=False)

    reason = "hyperliquid.balances:ledger_unimplemented"
    assert reason in _decision(expectations=expectations).reasons


@pytest.mark.parametrize("surface", ["orders", "positions"])
def test_order_and_position_identity_mismatch_is_unknown(surface: str) -> None:
    expected = _expectation()
    empty = frozenset()
    changed = _changed_expected(
        expected,
        surface,
        entities=replace(getattr(expected, surface).entities, fingerprints=empty),
        identities=replace(getattr(expected, surface).identities, fingerprints=empty),
    )
    expectations = {"hyperliquid": changed, "bybit": _expectation()}

    assert f"hyperliquid.{surface}:identity_mismatch" in _decision(
        expectations=expectations
    ).reasons


@pytest.mark.parametrize("surface", ["orders", "positions"])
def test_matching_identity_with_different_state_is_distinct(surface: str) -> None:
    expected = _expectation()
    current = getattr(expected, surface)
    changed = _changed_expected(
        expected,
        surface,
        entities=replace(current.entities, fingerprints=frozenset({"different-state"})),
    )
    expectations = {"hyperliquid": changed, "bybit": _expectation()}
    decision = _decision(expectations=expectations)

    assert f"hyperliquid.{surface}:state_mismatch" in decision.reasons
    assert f"hyperliquid.{surface}:identity_mismatch" not in decision.reasons


def test_every_local_fill_identity_must_exist_at_the_venue() -> None:
    expected = _expectation()
    changed = _changed_expected(
        expected,
        "fills",
        entities=replace(expected.fills.entities, fingerprints=frozenset({"missing-fill"})),
        identities=replace(expected.fills.identities, fingerprints=frozenset({"missing-id"})),
    )
    expectations = {"hyperliquid": changed, "bybit": _expectation()}

    assert "hyperliquid.fills:missing_local_fill" in _decision(
        expectations=expectations
    ).reasons


def test_matching_fill_identity_with_different_state_freezes() -> None:
    expected = _expectation().fills
    entities = replace(expected.entities, fingerprints=frozenset({"different-fill"}))

    reasons = _decision_with_expected("fills", entities=entities).reasons

    assert "hyperliquid.fills:fill_state_mismatch" in reasons
    assert "hyperliquid.fills:missing_local_fill" not in reasons


def test_balance_identity_mismatch_is_unknown() -> None:
    expected = _expectation().balances
    empty_entities = replace(expected.entities, fingerprints=frozenset())
    empty_identities = replace(expected.identities, fingerprints=frozenset())

    reasons = _decision_with_expected(
        "balances", entities=empty_entities, identities=empty_identities
    ).reasons

    assert "hyperliquid.balances:balance_identity_mismatch" in reasons


def test_matching_balance_identity_with_different_amount_freezes() -> None:
    expected = _expectation().balances
    entities = replace(expected.entities, fingerprints=frozenset({"different-amount"}))

    reasons = _decision_with_expected("balances", entities=entities).reasons

    assert "hyperliquid.balances:balance_state_mismatch" in reasons
    assert "hyperliquid.balances:balance_identity_mismatch" not in reasons
