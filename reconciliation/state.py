"""Pure, fail-closed startup reconciliation contracts."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

VENUES = frozenset({"hyperliquid", "bybit"})
SURFACES = ("orders", "fills", "positions", "balances")


class StartupContractError(ValueError):
    """Raised when startup evidence is structurally invalid."""


@dataclass(frozen=True, slots=True)
class CanonicalSet:
    scheme_id: str
    scheme_version: int
    fingerprints: frozenset[str]


@dataclass(frozen=True, slots=True)
class SurfaceEvidence:
    observed_ns: int
    fetched_count: int
    page_complete: bool
    truncated: bool
    unknown_count: int
    mismatch_count: int
    entities: CanonicalSet
    identities: CanonicalSet


@dataclass(frozen=True, slots=True)
class ExpectedSurface:
    entities: CanonicalSet
    identities: CanonicalSet


@dataclass(frozen=True, slots=True)
class VenueEvidence:
    orders: SurfaceEvidence
    fills: SurfaceEvidence
    positions: SurfaceEvidence
    balances: SurfaceEvidence


@dataclass(frozen=True, slots=True)
class VenueExpectation:
    orders: ExpectedSurface
    fills: ExpectedSurface
    positions: ExpectedSurface
    balances: ExpectedSurface
    frozen_intents: frozenset[str]
    balance_ledger_available: bool


@dataclass(frozen=True, slots=True)
class ValidatedStartup:
    startup_started_ns: int
    now_ns: int
    venues: tuple[tuple[str, VenueEvidence], ...]
    expectations: tuple[tuple[str, VenueExpectation], ...]


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    action: Literal["ready", "cancel_only_freeze"]
    reasons: tuple[str, ...]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StartupContractError(message)


def _valid_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_canonical(value: CanonicalSet) -> None:
    _require(isinstance(value, CanonicalSet), "invalid canonical set")
    _require(isinstance(value.scheme_id, str) and bool(value.scheme_id), "invalid scheme_id")
    valid_version = _valid_nonnegative_int(value.scheme_version) and value.scheme_version > 0
    _require(valid_version, "invalid scheme_version")
    _require(isinstance(value.fingerprints, frozenset), "invalid fingerprints")
    valid_members = all(isinstance(item, str) and bool(item) for item in value.fingerprints)
    _require(valid_members, "invalid fingerprint")


def validate_surface_evidence(evidence: SurfaceEvidence, now_ns: int) -> SurfaceEvidence:
    """Validate one complete venue query without issuing an admission decision."""
    _require(isinstance(evidence, SurfaceEvidence), "invalid surface evidence")
    _require(_valid_nonnegative_int(now_ns), "invalid now_ns")
    _require(_valid_nonnegative_int(evidence.observed_ns), "invalid observed_ns")
    _require(evidence.observed_ns <= now_ns, "observed_ns is in the future")
    for field in ("fetched_count", "unknown_count", "mismatch_count"):
        _require(_valid_nonnegative_int(getattr(evidence, field)), f"invalid {field}")
    _require(type(evidence.page_complete) is bool, "invalid page_complete")
    _require(type(evidence.truncated) is bool, "invalid truncated")
    _validate_canonical(evidence.entities)
    _validate_canonical(evidence.identities)
    _require(
        evidence.entities.scheme_id != evidence.identities.scheme_id,
        "identity and state schemes must be distinct",
    )
    _require(
        len(evidence.entities.fingerprints) == len(evidence.identities.fingerprints),
        "identity and state cardinality differ",
    )
    represented = len(evidence.entities.fingerprints) + evidence.unknown_count
    _require(evidence.fetched_count == represented, "fetched_count does not match entities")
    return evidence


def _validate_expected_surface(expected: ExpectedSurface) -> None:
    _require(isinstance(expected, ExpectedSurface), "invalid expected surface")
    _validate_canonical(expected.entities)
    _validate_canonical(expected.identities)
    _require(
        expected.entities.scheme_id != expected.identities.scheme_id,
        "expected identity and state schemes must be distinct",
    )
    _require(
        len(expected.entities.fingerprints) == len(expected.identities.fingerprints),
        "expected identity and state cardinality differ",
    )


def _require_matching_contracts(actual: SurfaceEvidence, expected: ExpectedSurface) -> None:
    pairs = ((actual.entities, expected.entities), (actual.identities, expected.identities))
    for venue_set, local_set in pairs:
        contract = (venue_set.scheme_id, venue_set.scheme_version)
        expected_contract = (local_set.scheme_id, local_set.scheme_version)
        _require(contract == expected_contract, "canonicalization contracts differ")


def _validate_expectation(expectation: VenueExpectation) -> None:
    _require(isinstance(expectation, VenueExpectation), "invalid expectation")
    for name in SURFACES:
        _validate_expected_surface(getattr(expectation, name))
    _require(isinstance(expectation.frozen_intents, frozenset), "invalid frozen_intents")
    valid_intents = all(
        isinstance(value, str) and bool(value) for value in expectation.frozen_intents
    )
    _require(valid_intents, "invalid frozen intent")
    _require(type(expectation.balance_ledger_available) is bool, "invalid balance ledger")


def validate_startup_structure(
    *,
    startup_started_ns: int,
    now_ns: int,
    venues: Mapping[str, VenueEvidence],
    expectations: Mapping[str, VenueExpectation],
) -> ValidatedStartup:
    """Validate two-venue startup structure without deciding admission."""
    valid_clock = _valid_nonnegative_int(startup_started_ns)
    valid_clock &= _valid_nonnegative_int(now_ns) and now_ns >= startup_started_ns
    _require(valid_clock, "startup clock invalid")
    _require(isinstance(venues, Mapping), "invalid evidence venues")
    _require(isinstance(expectations, Mapping), "invalid expectation venues")
    _require(set(venues) == VENUES, "invalid evidence venues")
    _require(set(expectations) == VENUES, "invalid expectation venues")
    for venue in sorted(VENUES):
        evidence = venues[venue]
        expectation = expectations[venue]
        _require(isinstance(evidence, VenueEvidence), "invalid venue evidence")
        _validate_expectation(expectation)
        for name in SURFACES:
            surface = validate_surface_evidence(getattr(evidence, name), now_ns)
            _require_matching_contracts(surface, getattr(expectation, name))
    return ValidatedStartup(
        startup_started_ns=startup_started_ns,
        now_ns=now_ns,
        venues=tuple((name, venues[name]) for name in sorted(VENUES)),
        expectations=tuple((name, expectations[name]) for name in sorted(VENUES)),
    )


def _surface_reasons(
    venue: str, name: str, surface: SurfaceEvidence, startup_started_ns: int
) -> list[str]:
    reasons = []
    if surface.observed_ns <= startup_started_ns:
        reasons.append(f"{venue}.{name}:stale")
    if not surface.page_complete:
        reasons.append(f"{venue}.{name}:pagination_incomplete")
    if surface.truncated:
        reasons.append(f"{venue}.{name}:truncated")
    if surface.unknown_count:
        reasons.append(f"{venue}.{name}:unknown_entities")
    if surface.mismatch_count:
        reasons.append(f"{venue}.{name}:mismatch")
    return reasons


def _decision(reasons: list[str]) -> AdmissionDecision:
    ordered = tuple(sorted(reasons))
    action = "cancel_only_freeze" if ordered else "ready"
    return AdmissionDecision(action=action, reasons=ordered)


def _validate_previous_freeze(value: AdmissionDecision | None) -> None:
    if value is None:
        return
    _require(isinstance(value, AdmissionDecision), "invalid previous freeze")
    _require(value.action == "cancel_only_freeze", "previous decision is not a freeze")
    _require(isinstance(value.reasons, tuple), "invalid previous freeze reasons")
    _require(all(isinstance(reason, str) and reason for reason in value.reasons), "invalid reason")


def _expectation_gate_reasons(
    expectations: dict[str, VenueExpectation],
) -> tuple[list[str], list[str]]:
    frozen = []
    ledger = []
    for venue in sorted(VENUES):
        expectation = expectations[venue]
        if expectation.frozen_intents:
            frozen.append(f"{venue}:frozen_intent")
        if not expectation.balance_ledger_available:
            ledger.append(f"{venue}.balances:ledger_unimplemented")
    return frozen, ledger


def _state_reasons(
    venues: dict[str, VenueEvidence], expectations: dict[str, VenueExpectation]
) -> list[str]:
    reasons = []
    for venue in sorted(VENUES):
        actual = venues[venue]
        expected = expectations[venue]
        for name in ("orders", "positions"):
            venue_surface = getattr(actual, name)
            local_surface = getattr(expected, name)
            if venue_surface.identities.fingerprints != local_surface.identities.fingerprints:
                reasons.append(f"{venue}.{name}:identity_mismatch")
            elif venue_surface.entities.fingerprints != local_surface.entities.fingerprints:
                reasons.append(f"{venue}.{name}:state_mismatch")
        if not expected.fills.identities.fingerprints <= actual.fills.identities.fingerprints:
            reasons.append(f"{venue}.fills:missing_local_fill")
    return reasons


def decide_startup_admission(
    *,
    startup_started_ns: int,
    now_ns: int,
    venues: Mapping[str, VenueEvidence],
    expectations: Mapping[str, VenueExpectation],
    previous_freeze: AdmissionDecision | None = None,
) -> AdmissionDecision:
    """Admit only complete, fresh, fully known four-surface evidence."""
    validated = validate_startup_structure(
        startup_started_ns=startup_started_ns,
        now_ns=now_ns,
        venues=venues,
        expectations=expectations,
    )
    validated_venues = dict(validated.venues)
    validated_expectations = dict(validated.expectations)
    _validate_previous_freeze(previous_freeze)
    if previous_freeze is not None:
        return _decision(["startup:previous_freeze"])
    frozen, ledger = _expectation_gate_reasons(validated_expectations)
    if frozen:
        return _decision(frozen)
    evidence_reasons = [
        reason
        for venue, evidence in validated.venues
        for name in SURFACES
        for reason in _surface_reasons(
            venue, name, getattr(evidence, name), validated.startup_started_ns
        )
    ]
    if evidence_reasons:
        return _decision(evidence_reasons)
    if ledger:
        return _decision(ledger)
    return _decision(_state_reasons(validated_venues, validated_expectations))
