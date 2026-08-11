import hashlib
from pathlib import Path

import pytest

from execution.writer import (
    WriterAuthority,
    WriterIdentity,
    WriterLease,
    WriterLeaseError,
)
from reconciliation.promotion import WriterPromotionDecision, promote_writer
from reconciliation.state import AdmissionDecision


def _identity() -> WriterIdentity:
    return WriterIdentity("hyperliquid:test", "writer-one", "a" * 64, "boot-one")


def _acquire(root: Path, acquired_ns: int = 100) -> WriterLease:
    return WriterLease.acquire(root, _identity(), [].append, acquired_ns=acquired_ns)


def _decision(**changes: object) -> WriterPromotionDecision:
    fields = {
        "account_digest": "a" * 64,
        "instance_id": "writer-one",
        "boot_id": "boot-one",
        "lease_epoch": 1,
        "from_mode": "pending_reconciliation",
        "to_mode": "risk_increasing",
        "outcome": "promoted",
        "reason": "admission_ready",
        "admission_action": "ready",
        "admission_digest": "b" * 64,
        "decided_ns": 100,
    }
    fields.update(changes)
    return WriterPromotionDecision(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {
            "to_mode": "pending_reconciliation", "outcome": "denied",
            "reason": "admission_freeze", "admission_action": "cancel_only_freeze",
        },
        {
            "from_mode": "cancel_only", "to_mode": "cancel_only", "outcome": "denied",
            "reason": "not_promotable_mode", "admission_action": "ready",
        },
        {
            "from_mode": "cancel_only", "to_mode": "cancel_only", "outcome": "denied",
            "reason": "not_promotable_mode", "admission_action": "cancel_only_freeze",
        },
    ],
)
def test_promotion_decision_accepts_only_closed_outcomes(changes: dict) -> None:
    assert _decision(**changes).decided_ns == 100


@pytest.mark.parametrize(
    "changes",
    [
        {"to_mode": "pending_reconciliation"},
        {"account_digest": "raw-account"},
        {"admission_digest": "raw-admission"},
        {"instance_id": ""},
        {"boot_id": ""},
        {"lease_epoch": 0},
        {"decided_ns": 0},
    ],
)
def test_promotion_decision_rejects_invalid_fields_and_combinations(changes: dict) -> None:
    with pytest.raises(ValueError):
        _decision(**changes)


@pytest.mark.parametrize("acquired_ns", [None, True, "100"])
def test_acquire_rejects_non_integer_time_before_lock_io(
    tmp_path: Path, acquired_ns: object
) -> None:
    with pytest.raises(TypeError, match="acquired_ns must be an integer"):
        WriterLease.acquire(tmp_path / "missing", _identity(), [].append, acquired_ns=acquired_ns)


@pytest.mark.parametrize("acquired_ns", [0, -1])
def test_acquire_rejects_non_positive_time_before_lock_io(
    tmp_path: Path, acquired_ns: int
) -> None:
    with pytest.raises(ValueError):
        WriterLease.acquire(tmp_path / "missing", _identity(), [].append, acquired_ns=acquired_ns)


def test_elevation_records_before_mutating_authority(tmp_path: Path) -> None:
    lease = _acquire(tmp_path)
    observed = []

    def before_elevate(authority: WriterAuthority) -> None:
        observed.append((authority, lease.authority.mode))

    elevated = lease.elevate_to_risk_increasing(
        promotion_ns=100, before_elevate=before_elevate
    )
    assert len(observed) == 1
    assert observed[0][1] == "pending_reconciliation"
    assert observed[0][0].mode == "pending_reconciliation"
    assert elevated == lease.authority
    assert elevated.mode == "risk_increasing"
    lease.release()


def test_elevation_recorder_failure_preserves_pending_authority(tmp_path: Path) -> None:
    lease = _acquire(tmp_path)
    failure = OSError("promotion evidence unavailable")

    def fail(_authority: WriterAuthority) -> None:
        raise failure

    with pytest.raises(OSError) as raised:
        lease.elevate_to_risk_increasing(promotion_ns=101, before_elevate=fail)
    assert raised.value is failure
    assert lease.authority.mode == "pending_reconciliation"
    lease.release()


@pytest.mark.parametrize("promotion_ns", [None, True, "100", 0, 99])
def test_elevation_rejects_invalid_or_backward_time(
    tmp_path: Path, promotion_ns: object
) -> None:
    lease = _acquire(tmp_path)
    callback_calls = []
    error = TypeError if promotion_ns in (None, True, "100") else ValueError
    with pytest.raises(error):
        lease.elevate_to_risk_increasing(
            promotion_ns=promotion_ns, before_elevate=callback_calls.append
        )
    assert callback_calls == []
    assert lease.authority.mode == "pending_reconciliation"
    lease.release()


def test_cancel_only_elevation_rejects_mode_before_time_comparison(tmp_path: Path) -> None:
    identity = _identity()
    authority = WriterAuthority(identity, "cancel_only", 1)
    path = WriterLease.path_for(tmp_path, identity.account_id)
    lease = WriterLease(path, authority, None, [].append, True, acquired_ns=None)
    with pytest.raises(WriterLeaseError, match="not promotable"):
        lease.elevate_to_risk_increasing(promotion_ns=1, before_elevate=lambda _: None)


@pytest.mark.parametrize(
    ("promotion_ns", "before_elevate", "message"),
    [("100", lambda _: None, "promotion_ns"), (100, None, "before_elevate")],
)
def test_structural_elevation_errors_precede_inode_revalidation(
    tmp_path: Path, promotion_ns: object, before_elevate: object, message: str
) -> None:
    lease = _acquire(tmp_path)
    lease.path.unlink()
    lease.path.touch()
    with pytest.raises(TypeError, match=message):
        lease.elevate_to_risk_increasing(
            promotion_ns=promotion_ns, before_elevate=before_elevate
        )


def test_ready_admission_records_exact_decision_and_promotes(tmp_path: Path) -> None:
    lease = _acquire(tmp_path)
    recorded = []
    authority = promote_writer(
        lease, AdmissionDecision("ready", ()), recorded.append, now_ns=101
    )
    expected = WriterPromotionDecision(
        hashlib.sha256(_identity().account_id.encode()).hexdigest(),
        "writer-one", "boot-one", 1, "pending_reconciliation", "risk_increasing",
        "promoted", "admission_ready", "ready",
        hashlib.sha256(b'{"action":"ready","reasons":[]}').hexdigest(), 101,
    )
    assert recorded == [expected]
    assert authority == lease.authority and authority.mode == "risk_increasing"
    lease.release()


def test_ready_recorder_failure_preserves_pending_mode(tmp_path: Path) -> None:
    lease = _acquire(tmp_path)
    failure = OSError("promotion record failed")
    observed = []

    def fail(_decision: WriterPromotionDecision) -> None:
        observed.append(lease.authority.mode)
        raise failure

    with pytest.raises(OSError) as raised:
        promote_writer(lease, AdmissionDecision("ready", ()), fail, now_ns=101)
    assert raised.value is failure and observed == ["pending_reconciliation"]
    assert lease.authority.mode == "pending_reconciliation"
    lease.release()


def test_freeze_records_denial_without_entering_elevation(tmp_path: Path) -> None:
    lease = _acquire(tmp_path, acquired_ns=100)
    recorded = []
    admission = AdmissionDecision("cancel_only_freeze", ("balance:mismatch",))
    with pytest.raises(WriterLeaseError, match="admission freeze"):
        promote_writer(lease, admission, recorded.append, now_ns=99)
    assert (recorded[0].from_mode, recorded[0].to_mode) == (
        "pending_reconciliation", "pending_reconciliation",
    )
    assert recorded[0].reason == "admission_freeze" and recorded[0].decided_ns == 99
    assert lease.authority.mode == "pending_reconciliation"
    lease.release()


@pytest.mark.parametrize("admission", [
    AdmissionDecision("ready", ()), AdmissionDecision("cancel_only_freeze", ("freeze",)),
])
def test_cancel_only_records_not_promotable_for_either_admission(
    tmp_path: Path, admission: AdmissionDecision
) -> None:
    identity = _identity()
    authority = WriterAuthority(identity, "cancel_only", 7)
    path = WriterLease.path_for(tmp_path, identity.account_id)
    lease = WriterLease(path, authority, None, [].append, True, acquired_ns=None)
    recorded = []
    with pytest.raises(WriterLeaseError, match="not promotable"):
        promote_writer(lease, admission, recorded.append, now_ns=1)
    assert recorded[0].reason == "not_promotable_mode"
    assert recorded[0].admission_action == admission.action
    assert lease.authority.mode == "cancel_only"


def test_repeat_promotion_rejects_before_inode_revalidation(tmp_path: Path) -> None:
    lease = _acquire(tmp_path)
    recorded = []
    admission = AdmissionDecision("ready", ())
    promote_writer(lease, admission, recorded.append, now_ns=101)
    recorded.clear()
    lease.path.unlink()
    lease.path.touch()
    with pytest.raises(TypeError, match="now_ns"):
        promote_writer(lease, admission, recorded.append, now_ns="102")
    with pytest.raises(WriterLeaseError, match="already risk increasing"):
        promote_writer(lease, admission, recorded.append, now_ns=102)
    assert recorded == [] and lease.authority.mode == "risk_increasing"


def test_inode_failure_records_only_lease_invalidation(tmp_path: Path) -> None:
    lease_records = []
    lease = WriterLease.acquire(
        tmp_path, _identity(), lease_records.append, acquired_ns=100
    )
    promotion_records = []
    lease.path.unlink()
    lease.path.touch()
    with pytest.raises(WriterLeaseError, match="inode changed"):
        promote_writer(
            lease, AdmissionDecision("ready", ()), promotion_records.append, now_ns=101
        )
    assert promotion_records == []
    assert lease_records[-1].outcome == "invalidated"


def test_denied_recorder_failure_replaces_writer_rejection(tmp_path: Path) -> None:
    lease = _acquire(tmp_path)
    failure = OSError("denial record failed")

    def fail(_decision: WriterPromotionDecision) -> None:
        raise failure

    admission = AdmissionDecision("cancel_only_freeze", ("freeze",))
    with pytest.raises(OSError) as raised:
        promote_writer(lease, admission, fail, now_ns=101)
    assert raised.value is failure
    assert lease.authority.mode == "pending_reconciliation"
    lease.release()
