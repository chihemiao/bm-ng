import ast
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from data import contracts
from execution import wallet as wallet_module
from execution.wallet import (
    ROTATION_LEAD_NS,
    VALIDITY_NS,
    AgentWalletRegistration,
    assess,
)

DAY_NS = 86_400 * 1_000_000_000
ISSUED_NS = 1_000_000_000
ROOT = Path(__file__).parents[1]
VALID_ROTATION_COMBINATIONS = (
    ("rotated", "rotation_completed", "rotation_due"),
    ("rotated", "rotation_completed", "expired"),
    ("aborted", "not_due", "active"),
    ("aborted", "same_wallet", "active"),
    ("aborted", "same_wallet", "rotation_due"),
    ("aborted", "same_wallet", "expired"),
    ("aborted", "identity_changed", "active"),
    ("aborted", "identity_changed", "rotation_due"),
    ("aborted", "identity_changed", "expired"),
    ("aborted", "release_failed", "rotation_due"),
    ("aborted", "release_failed", "expired"),
    ("aborted", "acquire_failed", "rotation_due"),
    ("aborted", "acquire_failed", "expired"),
)


def _registration(**changes: object) -> AgentWalletRegistration:
    values = {
        "wallet_fingerprint": "a" * 64,
        "issued_ns": ISSUED_NS,
        "expires_ns": ISSUED_NS + VALIDITY_NS,
    }
    values.update(changes)
    return AgentWalletRegistration(**values)


def _rotation_event(**changes: object) -> dict:
    payload = {
        "account_digest": "a" * 64,
        "instance_id": "writer-one",
        "boot_id": "identity-boot",
        "old_wallet_fingerprint": "b" * 64,
        "new_wallet_fingerprint": "c" * 64,
        "old_issued_ns": ISSUED_NS,
        "old_expires_ns": ISSUED_NS + VALIDITY_NS,
        "new_issued_ns": ISSUED_NS + DAY_NS,
        "new_expires_ns": ISSUED_NS + DAY_NS + VALIDITY_NS,
        "assessment": "rotation_due",
        "outcome": "rotated",
        "reason": "rotation_completed",
        "decided_ns": ISSUED_NS + VALIDITY_NS - ROTATION_LEAD_NS,
    }
    payload.update(changes)
    return {
        "schema_ver": 1,
        "event_kind": "decision",
        "payload_schema": "agent_wallet_rotation",
        "venue": "local",
        "conn_id": "wallet-lifecycle",
        "boot_id": "recorder-boot",
        "recv_wall_ns": payload["decided_ns"],
        "recv_mono_ns": 1,
        "source": "execution",
        "seq_within_boot": 1,
        "payload": payload,
    }


def _decided_ns(assessment: str) -> int:
    expiry = ISSUED_NS + VALIDITY_NS
    return {
        "active": expiry - ROTATION_LEAD_NS - 1,
        "rotation_due": expiry - ROTATION_LEAD_NS,
        "expired": expiry,
    }[assessment]


def _rotation_decision(**changes: object):
    payload = _rotation_event(**changes)["payload"]
    return wallet_module.AgentWalletRotationDecision(**payload)


def test_wallet_timing_constants_are_owned_by_the_data_contract() -> None:
    assert contracts.VALIDITY_NS == VALIDITY_NS == 30 * DAY_NS
    assert contracts.ROTATION_LEAD_NS == ROTATION_LEAD_NS == 7 * DAY_NS

    tree = ast.parse((ROOT / "execution" / "wallet.py").read_text())
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert {"VALIDITY_NS", "ROTATION_LEAD_NS"}.isdisjoint(assigned)
    assert wallet_module.VALIDITY_NS == contracts.VALIDITY_NS


def test_wallet_rotation_schema_is_registered_and_accepts_identity_boot_separation() -> None:
    event = _rotation_event()

    assert "agent_wallet_rotation" in contracts.PAYLOAD_SCHEMAS
    assert len(contracts.PAYLOAD_SCHEMAS) == 20
    assert event["boot_id"] != event["payload"]["boot_id"]
    assert contracts.validate_envelope(event) is event


def test_wallet_rotation_requires_versioned_durable_exact_fields() -> None:
    event = _rotation_event()
    event["event_kind"] = "ops"
    with pytest.raises(contracts.ContractError, match="event kind"):
        contracts.validate_envelope(event)
    event = _rotation_event()
    event["schema_ver"] = 2
    with pytest.raises(contracts.ContractError, match="schema version"):
        contracts.validate_envelope(event)
    event = _rotation_event()
    del event["seq_within_boot"]
    with pytest.raises(contracts.ContractError, match="seq_within_boot"):
        contracts.validate_envelope(event)
    with pytest.raises(contracts.ContractError, match="fields"):
        contracts.validate_envelope(_rotation_event(unexpected=True))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"account_digest": "raw-account"}, "account_digest"),
        ({"old_wallet_fingerprint": "B" * 64}, "old_wallet_fingerprint"),
        ({"new_wallet_fingerprint": "c" * 63}, "new_wallet_fingerprint"),
        ({"instance_id": ""}, "instance_id"),
        ({"boot_id": ""}, "boot_id"),
        ({"old_issued_ns": True}, "old_issued_ns"),
        ({"old_expires_ns": 0}, "old_expires_ns"),
        ({"new_issued_ns": 1.0}, "new_issued_ns"),
        ({"new_expires_ns": -1}, "new_expires_ns"),
        ({"decided_ns": 0}, "decided_ns"),
        ({"assessment": "unknown"}, "assessment"),
        ({"outcome": "unknown"}, "outcome"),
        ({"reason": "unknown"}, "reason"),
    ],
)
def test_wallet_rotation_rejects_invalid_field_formats(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(contracts.ContractError, match=message):
        contracts.validate_envelope(_rotation_event(**changes))


@pytest.mark.parametrize(
    ("outcome", "reason", "assessment"),
    VALID_ROTATION_COMBINATIONS,
)
def test_wallet_rotation_accepts_every_closed_semantic_combination(
    outcome: str, reason: str, assessment: str
) -> None:
    changes = {
        "outcome": outcome,
        "reason": reason,
        "assessment": assessment,
        "decided_ns": _decided_ns(assessment),
    }
    if reason == "same_wallet":
        changes["new_wallet_fingerprint"] = "b" * 64

    event = _rotation_event(**changes)
    assert contracts.validate_envelope(event) is event


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"old_expires_ns": ISSUED_NS + VALIDITY_NS - 1}, "old wallet validity"),
        (
            {"new_expires_ns": ISSUED_NS + DAY_NS + VALIDITY_NS - 1},
            "new wallet validity",
        ),
        ({"decided_ns": ISSUED_NS - 1, "assessment": "active"}, "predates"),
        (
            {"assessment": "active", "decided_ns": _decided_ns("rotation_due")},
            "assessment",
        ),
        ({"assessment": "rotation_due", "decided_ns": _decided_ns("expired")}, "assessment"),
        (
            {"assessment": "expired", "decided_ns": _decided_ns("expired") - 1},
            "assessment",
        ),
        ({"outcome": "aborted", "reason": "same_wallet"}, "fingerprint"),
        (
            {
                "outcome": "aborted",
                "reason": "release_failed",
                "new_wallet_fingerprint": "b" * 64,
            },
            "fingerprint",
        ),
        ({"outcome": "aborted", "reason": "not_due"}, "combination"),
        (
            {
                "outcome": "aborted",
                "reason": "release_failed",
                "assessment": "active",
                "decided_ns": _decided_ns("active"),
            },
            "combination",
        ),
        (
            {"assessment": "active", "decided_ns": _decided_ns("active")},
            "combination",
        ),
    ],
)
def test_wallet_rotation_rejects_semantic_contradictions(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(contracts.ContractError, match=message):
        contracts.validate_envelope(_rotation_event(**changes))


@pytest.mark.parametrize(("outcome", "reason", "assessment"), VALID_ROTATION_COMBINATIONS)
def test_rotation_decision_accepts_every_schema_combination(
    outcome: str, reason: str, assessment: str
) -> None:
    changes = {
        "outcome": outcome,
        "reason": reason,
        "assessment": assessment,
        "decided_ns": _decided_ns(assessment),
    }
    if reason == "same_wallet":
        changes["new_wallet_fingerprint"] = "b" * 64
    assert _rotation_decision(**changes).reason == reason


def test_rotation_decision_is_frozen_and_matches_all_thirteen_schema_fields() -> None:
    decision = _rotation_decision()
    assert tuple(field.name for field in fields(decision)) == (
        "account_digest", "instance_id", "boot_id",
        "old_wallet_fingerprint", "new_wallet_fingerprint",
        "old_issued_ns", "old_expires_ns", "new_issued_ns", "new_expires_ns",
        "assessment", "outcome", "reason", "decided_ns",
    )
    with pytest.raises(FrozenInstanceError):
        decision.reason = "not_due"


@pytest.mark.parametrize(
    "changes",
    [
        {"account_digest": "raw-account"},
        {"old_wallet_fingerprint": "B" * 64},
        {"new_wallet_fingerprint": "c" * 63},
        {"instance_id": ""},
        {"boot_id": ""},
        {
            "old_issued_ns": True,
            "old_expires_ns": 1 + VALIDITY_NS,
            "decided_ns": 1 + VALIDITY_NS - ROTATION_LEAD_NS,
        },
        {"old_expires_ns": 0},
        {"new_issued_ns": 1.0},
        {"new_expires_ns": 0},
        {"decided_ns": 0},
        {"old_expires_ns": ISSUED_NS + VALIDITY_NS - 1},
        {"outcome": "aborted", "reason": "not_due"},
    ],
)
def test_rotation_decision_rejects_invalid_structure_and_semantics(
    changes: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        _rotation_decision(**changes)


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
