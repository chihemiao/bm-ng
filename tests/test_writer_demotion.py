from pathlib import Path

import pytest

from data.contracts import ContractError, validate_envelope
from execution.writer import WriterAuthority, WriterIdentity, WriterLease, WriterLeaseError
from reconciliation.admission import CONTINUOUS_ADMISSION_REASON_KEYS
from reconciliation.promotion import demote_writer, demotion_reason
from reconciliation.state import AdmissionDecision

NONCE_KEY = "continuous_admission:nonce_frozen"


def _freeze(*reasons: str) -> AdmissionDecision:
    return AdmissionDecision("cancel_only_freeze", tuple(sorted(reasons)))


def _identity() -> WriterIdentity:
    return WriterIdentity("hyperliquid:test", "writer-one", "a" * 64, "boot-one")


def _lease_for_mode(root: Path, mode: str, recorder) -> WriterLease:
    identity = _identity()
    if mode == "cancel_only":
        authority = WriterAuthority(identity, mode, 1)
        return WriterLease(
            WriterLease.path_for(root, identity.account_id),
            authority,
            None,
            recorder,
            True,
            acquired_ns=None,
        )
    lease = WriterLease.acquire(root, identity, recorder, acquired_ns=100)
    lease._authority = lease.authority._replace(mode=mode)
    return lease


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


def test_freeze_demotes_and_emits_schema_valid_decision(tmp_path: Path) -> None:
    recorded = []
    lease = _lease_for_mode(tmp_path, "risk_increasing", recorded.append)
    recorded.clear()
    authority = demote_writer(lease, _freeze("continuous_admission:pair_unknown"), now_ns=101)
    assert authority == lease.authority and authority.mode == "cancel_only"
    assert len(recorded) == 1
    assert (recorded[0].action, recorded[0].outcome) == ("demote", "cancel_only")
    event = _writer_event(recorded[0].reason)
    event["payload"] = recorded[0]._asdict()
    assert validate_envelope(event)["payload"]["reason"] == recorded[0].reason
    with pytest.raises(WriterLeaseError, match="not authorized"):
        lease.authorize("submit")
    lease.release()


def test_demotion_records_only_after_mode_is_cancel_only(tmp_path: Path) -> None:
    holder = {}
    observed_modes = []

    def record(decision) -> None:
        if decision.action == "demote":
            observed_modes.append(holder["lease"].authority.mode)

    lease = _lease_for_mode(tmp_path, "risk_increasing", record)
    holder["lease"] = lease
    demote_writer(lease, _freeze("continuous_admission:exposure_unknown"), now_ns=101)
    assert observed_modes == ["cancel_only"]
    lease.release()


def test_demote_writer_records_every_encoded_reason(tmp_path: Path) -> None:
    recorded = []
    lease = _lease_for_mode(tmp_path, "risk_increasing", recorded.append)
    recorded.clear()
    admission = _freeze(
        "continuous_admission:exposure_unknown",
        "continuous_admission:nonce_frozen:nonce-conflict",
        "continuous_admission:pair_unknown",
    )
    demote_writer(lease, admission, now_ns=101)
    assert recorded[0].reason == demotion_reason(admission)
    lease.release()


def test_recorder_failure_reports_demotion_already_applied(tmp_path: Path) -> None:
    failure = OSError("decision stream unavailable")

    def fail_demotion(decision) -> None:
        if decision.action == "demote":
            raise failure

    lease = _lease_for_mode(tmp_path, "risk_increasing", fail_demotion)
    with pytest.raises(WriterLeaseError, match="demotion applied.*evidence") as caught:
        demote_writer(lease, _freeze("continuous_admission:pair_unknown"), now_ns=101)
    assert caught.value.__cause__ is failure
    assert lease.authority.mode == "cancel_only"
    lease.release()


def test_ready_decision_cannot_demote_or_record(tmp_path: Path) -> None:
    recorded = []
    lease = _lease_for_mode(tmp_path, "risk_increasing", recorded.append)
    recorded.clear()
    with pytest.raises(ValueError, match="ready"):
        demote_writer(lease, AdmissionDecision("ready", ()), now_ns=101)
    assert lease.authority.mode == "risk_increasing" and recorded == []
    lease.release()


@pytest.mark.parametrize("mode", ["pending_reconciliation", "cancel_only"])
def test_non_risk_modes_are_idempotent_without_recording(tmp_path: Path, mode: str) -> None:
    recorded = []
    lease = _lease_for_mode(tmp_path, mode, recorded.append)
    recorded.clear()
    authority = demote_writer(lease, _freeze("continuous_admission:pair_unknown"), now_ns=101)
    assert authority.mode == mode and recorded == []
    lease.release()


def test_demotion_requires_real_lease_and_admission(tmp_path: Path) -> None:
    admission = _freeze("continuous_admission:pair_unknown")
    with pytest.raises(TypeError, match="lease"):
        demote_writer(object(), admission, now_ns=101)  # type: ignore[arg-type]
    lease = _lease_for_mode(tmp_path, "risk_increasing", [].append)
    with pytest.raises(TypeError, match="admission"):
        demote_writer(lease, ("freeze",), now_ns=101)  # type: ignore[arg-type]
    lease.release()


@pytest.mark.parametrize(
    ("now_ns", "error"),
    [(None, TypeError), (True, TypeError), ("101", TypeError), (0, ValueError), (-1, ValueError)],
)
def test_demotion_rejects_invalid_time_before_state_change(
    tmp_path: Path, now_ns: object, error: type[Exception]
) -> None:
    recorded = []
    lease = _lease_for_mode(tmp_path, "risk_increasing", recorded.append)
    recorded.clear()
    with pytest.raises(error):
        demote_writer(lease, _freeze("continuous_admission:pair_unknown"), now_ns=now_ns)
    assert lease.authority.mode == "risk_increasing" and recorded == []
    lease.release()


@pytest.mark.parametrize(
    ("reason", "error"),
    [(None, TypeError), ("", ValueError), ("continuous_admission:pair_unknown", ValueError)],
)
def test_low_level_demotion_rejects_invalid_reason_before_state_change(
    tmp_path: Path, reason: object, error: type[Exception]
) -> None:
    lease = _lease_for_mode(tmp_path, "risk_increasing", [].append)
    with pytest.raises(error):
        lease.demote_to_cancel_only(demotion_ns=101, reason=reason)
    assert lease.authority.mode == "risk_increasing"
    lease.release()
