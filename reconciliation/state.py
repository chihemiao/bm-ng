"""Pure, fail-closed startup reconciliation contracts."""

from dataclasses import dataclass


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
