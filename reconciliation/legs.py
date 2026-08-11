"""Fail-closed completion states for one execution leg."""

from decimal import Decimal

from reconciliation.state import (
    SurfaceEvidence,
    surface_is_authoritative,
    validate_surface_evidence,
)


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _validate_quantities(intended_quantity: Decimal, filled_quantity: Decimal | None) -> None:
    if type(intended_quantity) is not Decimal:
        raise TypeError("intended_quantity must be Decimal")
    if filled_quantity is not None and type(filled_quantity) is not Decimal:
        raise TypeError("filled_quantity must be Decimal or None")
    if not intended_quantity.is_finite():
        raise ValueError("intended_quantity must be finite")
    if filled_quantity is not None and not filled_quantity.is_finite():
        raise ValueError("filled_quantity must be finite")
    if intended_quantity <= 0:
        raise ValueError("intended_quantity must be positive")
    if filled_quantity is not None and filled_quantity < 0:
        raise ValueError("filled_quantity must be non-negative")


def leg_completion(
    *,
    intended_quantity: Decimal,
    filled_quantity: Decimal | None,
    evidence: SurfaceEvidence,
    now_ns: int,
    max_age_ns: int,
) -> str:
    """Classify one leg without treating incomplete evidence as completion."""
    _validate_quantities(intended_quantity, filled_quantity)
    now = _positive_int(now_ns, "now_ns")
    max_age = _positive_int(max_age_ns, "max_age_ns")
    if filled_quantity is None:
        return "unknown"
    observed_ns = evidence.observed_ns if isinstance(evidence, SurfaceEvidence) else 0
    validate_surface_evidence(evidence, now_ns=observed_ns)
    if not surface_is_authoritative(evidence, now_ns=now, max_age_ns=max_age):
        return "unknown"
    if filled_quantity == 0:
        return "none"
    if filled_quantity < intended_quantity:
        return "partial"
    if filled_quantity == intended_quantity:
        return "complete"
    return "overfilled"
