from dataclasses import FrozenInstanceError

import pytest

from execution.wallet import (
    ROTATION_LEAD_NS,
    VALIDITY_NS,
    AgentWalletRegistration,
    assess,
)

DAY_NS = 86_400 * 1_000_000_000
ISSUED_NS = 1_000_000_000


def _registration(**changes: object) -> AgentWalletRegistration:
    values = {
        "wallet_fingerprint": "a" * 64,
        "issued_ns": ISSUED_NS,
        "expires_ns": ISSUED_NS + VALIDITY_NS,
    }
    values.update(changes)
    return AgentWalletRegistration(**values)


def test_registration_is_a_frozen_record_with_fixed_validity() -> None:
    registration = _registration()

    assert VALIDITY_NS == 30 * DAY_NS
    assert ROTATION_LEAD_NS == 7 * DAY_NS
    with pytest.raises(FrozenInstanceError):
        registration.expires_ns += 1


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"wallet_fingerprint": "a" * 63}, "wallet_fingerprint"),
        ({"wallet_fingerprint": "A" * 64}, "wallet_fingerprint"),
        ({"issued_ns": True}, "issued_ns"),
        ({"issued_ns": 0}, "issued_ns"),
        ({"expires_ns": ISSUED_NS + VALIDITY_NS - 1}, "expires_ns"),
    ],
)
def test_registration_rejects_invalid_structure(changes: dict[str, object], message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        _registration(**changes)


@pytest.mark.parametrize("expires_ns", [float(ISSUED_NS + VALIDITY_NS), True])
def test_registration_requires_an_exact_integer_expiry(expires_ns: object) -> None:
    with pytest.raises(TypeError, match="expires_ns"):
        _registration(expires_ns=expires_ns)


@pytest.mark.parametrize(
    ("now_ns", "expected"),
    [
        (ISSUED_NS, "active"),
        (ISSUED_NS + VALIDITY_NS - ROTATION_LEAD_NS - 1, "active"),
        (ISSUED_NS + VALIDITY_NS - ROTATION_LEAD_NS, "rotation_due"),
        (ISSUED_NS + VALIDITY_NS - 1, "rotation_due"),
        (ISSUED_NS + VALIDITY_NS, "expired"),
    ],
)
def test_assess_uses_left_closed_rotation_and_expiry_boundaries(
    now_ns: int, expected: str
) -> None:
    assert assess(_registration(), now_ns) == expected


def test_assess_rejects_clock_rollback_and_non_integer_time() -> None:
    with pytest.raises(ValueError, match="predates"):
        assess(_registration(), ISSUED_NS - 1)
    with pytest.raises(TypeError, match="now_ns"):
        assess(_registration(), True)


def test_assess_rejects_an_uncontrolled_registration_type() -> None:
    with pytest.raises(TypeError, match="registration"):
        assess(object(), ISSUED_NS)
