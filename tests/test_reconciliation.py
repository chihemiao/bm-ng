from dataclasses import replace

import pytest

from reconciliation.state import (
    CanonicalSet,
    ExpectedSurface,
    StartupContractError,
    SurfaceEvidence,
    ValidatedStartup,
    VenueEvidence,
    VenueExpectation,
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


def _startup(**changes) -> ValidatedStartup:
    values = {
        "startup_started_ns": 100,
        "now_ns": 200,
        "venues": {"hyperliquid": _venue(), "bybit": _venue()},
        "expectations": {"hyperliquid": _expectation(), "bybit": _expectation()},
    }
    values.update(changes)
    return validate_startup_structure(**values)


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
