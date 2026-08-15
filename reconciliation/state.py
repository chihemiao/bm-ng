"""Pure, fail-closed startup reconciliation contracts."""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from execution.nonce import replay_freeze_reason, replay_signer_nonce_conflict
from reconciliation.ledger import BalanceLedger, LedgerContractError, validate_balance_ledger

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


def canonical_fingerprint(value: Mapping[str, object]) -> str:
    """Hash a canonical JSON object for cross-venue reconciliation sets."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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
    balance_ledger: BalanceLedger | None


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

    def __post_init__(self) -> None:
        _require(self.action in {"ready", "cancel_only_freeze"}, "invalid admission action")
        _require(isinstance(self.reasons, tuple), "invalid admission reasons")
        valid = all(isinstance(reason, str) and reason for reason in self.reasons)
        _require(valid, "invalid admission reason")
        canonical = tuple(sorted(set(self.reasons)))
        _require(self.reasons == canonical, "admission reasons are not canonical")
        if self.action == "ready":
            _require(not self.reasons, "ready admission cannot have reasons")
        else:
            _require(bool(self.reasons), "freeze admission needs a reason")


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


def surface_is_authoritative(evidence: SurfaceEvidence, *, now_ns: int, max_age_ns: int) -> bool:
    """Report whether validated evidence is complete, known, and fresh."""
    age_ns = now_ns - evidence.observed_ns
    return (
        evidence.page_complete and not evidence.truncated
        and evidence.unknown_count == evidence.mismatch_count == 0
        and 0 <= age_ns <= max_age_ns
    )


def orders_surface_confirmed_empty(
    evidence: SurfaceEvidence, venue: str, *, now_ns: int, max_age_ns: int) -> bool:
    """Return whether valid venue order evidence authoritatively proves empty."""
    if type(venue) is not str or type(max_age_ns) is not int:
        raise TypeError("orders policy must use typed values")
    if not venue or max_age_ns <= 0:
        raise ValueError("orders policy values must be positive")
    validate_surface_evidence(evidence, now_ns)
    schemes = (evidence.entities.scheme_id, evidence.identities.scheme_id)
    if schemes != (f"{venue}.orders.state", f"{venue}.orders.identity"):
        raise ValueError(f"{venue} orders evidence scheme mismatch")
    empty = evidence.fetched_count == 0
    empty &= evidence.entities.fingerprints == evidence.identities.fingerprints == frozenset()
    return surface_is_authoritative(evidence, now_ns=now_ns, max_age_ns=max_age_ns) and empty


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
    ledger = expectation.balance_ledger
    _require(ledger is None or isinstance(ledger, BalanceLedger), "invalid balance ledger")
    if ledger is not None:
        try:
            validate_balance_ledger(ledger)
        except LedgerContractError as error:
            raise StartupContractError("invalid balance ledger") from error


def _validate_venue_maps(
    now_ns: int,
    venues: Mapping[str, VenueEvidence],
    expectations: Mapping[str, VenueExpectation],
) -> None:
    _require(_valid_nonnegative_int(now_ns), "invalid now_ns")
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


def validate_startup_structure(
    *, startup_started_ns: int, now_ns: int,
    venues: Mapping[str, VenueEvidence], expectations: Mapping[str, VenueExpectation],
) -> ValidatedStartup:
    """Validate two-venue startup structure without deciding admission."""
    valid_clock = _valid_nonnegative_int(startup_started_ns)
    valid_clock &= _valid_nonnegative_int(now_ns) and now_ns >= startup_started_ns
    _require(valid_clock, "startup clock invalid")
    _validate_venue_maps(now_ns, venues, expectations)
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
        audit = expectation.balance_ledger
        if audit is None:
            ledger.append(f"{venue}.balances:ledger_unavailable")
        else:
            if audit.unknown_entry_ids:
                ledger.append(f"{venue}.balances:ledger_unknown_entry")
            if not audit.self_consistent:
                ledger.append(f"{venue}.balances:ledger_inconsistent")
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
        elif not expected.fills.entities.fingerprints <= actual.fills.entities.fingerprints:
            reasons.append(f"{venue}.fills:fill_state_mismatch")
        if actual.balances.identities.fingerprints != expected.balances.identities.fingerprints:
            reasons.append(f"{venue}.balances:balance_identity_mismatch")
        elif actual.balances.entities.fingerprints != expected.balances.entities.fingerprints:
            reasons.append(f"{venue}.balances:balance_state_mismatch")
        audit = expected.balance_ledger
        if audit is not None and audit.end_ns != actual.balances.observed_ns:
            reasons.append(f"{venue}.balances:ledger_coverage_mismatch")
    return reasons


def classify_reconciliation_consistency(
    *, now_ns: int, max_age_ns: int,
    venues: Mapping[str, VenueEvidence], expectations: Mapping[str, VenueExpectation],
) -> bool | None:
    """Return None when evidence does not produce a streak input."""
    _require(_valid_nonnegative_int(max_age_ns), "invalid max_age_ns")
    _validate_venue_maps(now_ns, venues, expectations)
    authoritative = all(
        surface_is_authoritative(
            getattr(evidence, name), now_ns=now_ns, max_age_ns=max_age_ns)
        for evidence in venues.values() for name in SURFACES
    )
    if not authoritative:
        return None
    ledgers = (expectations[venue].balance_ledger for venue in sorted(VENUES))
    if any(
        ledger is None or ledger.unknown_entry_ids or not ledger.self_consistent
        for ledger in ledgers
    ):
        return None
    return not _state_reasons(dict(venues), dict(expectations))


def _signer_nonce_reasons(
    events: Iterable[Mapping[str, object]],
    wallet_fingerprint: str,
) -> tuple[str, ...]:
    rows = tuple(events)
    frozen = replay_freeze_reason(rows, wallet_fingerprint)
    conflict = replay_signer_nonce_conflict(rows, wallet_fingerprint)
    reasons = []
    if frozen is not None:
        reasons.append(f"signer_nonce_allocation:frozen:{frozen}")
    if conflict is not None:
        reasons.append(conflict)
    return tuple(sorted(set(reasons)))


def decide_startup_admission(
    *,
    startup_started_ns: int,
    now_ns: int,
    venues: Mapping[str, VenueEvidence],
    expectations: Mapping[str, VenueExpectation],
    signer_nonce_events: Iterable[Mapping[str, object]],
    signer_wallet_fingerprint: str,
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
    nonce_reasons = list(_signer_nonce_reasons(signer_nonce_events, signer_wallet_fingerprint))
    reasons = nonce_reasons
    if previous_freeze is not None:
        reasons.append("startup:previous_freeze")
    frozen, ledger = _expectation_gate_reasons(validated_expectations)
    if frozen:
        return _decision([*reasons, *frozen])
    evidence_reasons = [
        reason
        for venue, evidence in validated.venues
        for name in SURFACES
        for reason in _surface_reasons(
            venue, name, getattr(evidence, name), validated.startup_started_ns
        )
    ]
    if evidence_reasons:
        return _decision([*reasons, *evidence_reasons])
    if ledger:
        return _decision([*reasons, *ledger])
    return _decision([*reasons, *_state_reasons(validated_venues, validated_expectations)])
