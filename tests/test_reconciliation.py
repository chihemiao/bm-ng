from dataclasses import replace

import pytest

from reconciliation.state import (
    CanonicalSet,
    StartupContractError,
    SurfaceEvidence,
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
