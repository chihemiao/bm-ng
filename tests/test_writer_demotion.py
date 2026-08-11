import pytest

from data.contracts import ContractError, validate_envelope
from reconciliation.admission import CONTINUOUS_ADMISSION_REASON_KEYS
from reconciliation.promotion import demotion_reason
from reconciliation.state import AdmissionDecision

NONCE_KEY = "continuous_admission:nonce_frozen"


def _freeze(*reasons: str) -> AdmissionDecision:
    return AdmissionDecision("cancel_only_freeze", tuple(sorted(reasons)))


def _writer_event(reason: str) -> dict:
    return {
        "schema_ver": 1,
        "event_kind": "decision",
        "payload_schema": "writer_lease_decision",
        "venue": "hyperliquid",
        "conn_id": "writer-one",
        "boot_id": "boot-one",
        "recv_wall_ns": 1_000,
        "recv_mono_ns": 900,
        "source": "writer_lease",
        "seq_within_boot": 1,
        "payload": {
            "action": "demote",
            "outcome": "cancel_only",
            "reason": reason,
            "account_digest": "a" * 64,
            "instance_id": "writer-one",
            "wallet_fingerprint": "b" * 64,
            "boot_id": "boot-one",
            "lease_epoch": 1,
            "lock_path_digest": "c" * 64,
            "prior_epoch_valid": True,
        },
    }


@pytest.mark.parametrize("key", sorted(CONTINUOUS_ADMISSION_REASON_KEYS))
def test_demotion_reason_encodes_each_closed_key(key: str) -> None:
    reason = f"{key}:nonce-conflict" if key == NONCE_KEY else key
    assert demotion_reason(_freeze(reason)) == f"writer_demoted:{key}"


def test_demotion_reason_encodes_multiple_keys_and_drops_nonce_payload() -> None:
    reasons = (
        "continuous_admission:pair_unknown",
        "continuous_admission:nonce_frozen:allocated_nonce_conflict",
        "continuous_admission:exposure_unknown",
    )
    expected = (
        "writer_demoted:continuous_admission:exposure_unknown,"
        "continuous_admission:nonce_frozen,continuous_admission:pair_unknown"
    )
    assert demotion_reason(_freeze(*reasons)) == expected


def test_demotion_reason_inherits_order_from_admission_decision() -> None:
    decision = _freeze(
        "continuous_admission:pair_unknown",
        "continuous_admission:nonce_frozen:nonce-conflict",
        "continuous_admission:exposure_unknown",
    )
    encoded_keys = demotion_reason(decision).removeprefix("writer_demoted:").split(",")
    expected_keys = [reason.removesuffix(":nonce-conflict") for reason in decision.reasons]
    assert encoded_keys == expected_keys


def test_demotion_reason_rejects_ready_and_unknown_reason() -> None:
    with pytest.raises(ValueError):
        demotion_reason(AdmissionDecision("ready", ()))
    with pytest.raises(ValueError):
        demotion_reason(_freeze("continuous_admission:invented"))


def test_demotion_reason_rejects_non_decision() -> None:
    with pytest.raises(TypeError):
        demotion_reason(("continuous_admission:pair_unknown",))  # type: ignore[arg-type]


def test_demotion_reason_maximum_length_is_exact_and_bounded() -> None:
    reasons = tuple(
        f"{key}:detail" if key == NONCE_KEY else key for key in CONTINUOUS_ADMISSION_REASON_KEYS
    )
    encoded = demotion_reason(_freeze(*reasons))
    maximum = len("writer_demoted:") + sum(map(len, CONTINUOUS_ADMISSION_REASON_KEYS))
    maximum += len(CONTINUOUS_ADMISSION_REASON_KEYS) - 1
    assert len(encoded) == maximum


def test_schema_accepts_canonical_demotion_reason() -> None:
    reason = demotion_reason(_freeze("continuous_admission:pair_unknown"))
    assert validate_envelope(_writer_event(reason))["payload"]["action"] == "demote"


@pytest.mark.parametrize(
    "reason",
    [
        "writer_demoted:",
        "writer_demoted:continuous_admission:pair_unknown,continuous_admission:pair_unknown",
        "writer_demoted:continuous_admission:pair_unknown:payload",
        "writer_demoted:continuous_admission:pair_unknown, continuous_admission:exposure_unknown",
    ],
)
def test_schema_rejects_noncanonical_demotion_reason(reason: str) -> None:
    with pytest.raises(ContractError, match="writer decision combination"):
        validate_envelope(_writer_event(reason))
