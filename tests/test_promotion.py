from pathlib import Path

import pytest

from execution.writer import (
    WriterAuthority,
    WriterIdentity,
    WriterLease,
    WriterLeaseError,
)


def _identity() -> WriterIdentity:
    return WriterIdentity("hyperliquid:test", "writer-one", "a" * 64, "boot-one")


def _acquire(root: Path, acquired_ns: int = 100) -> WriterLease:
    return WriterLease.acquire(root, _identity(), [].append, acquired_ns=acquired_ns)


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
