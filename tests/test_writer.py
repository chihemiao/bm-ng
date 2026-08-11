import hashlib
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from data.shard import ShardWriter, replay_event_window
from execution.writer import (
    AUTHORITY_MODES,
    WriterAuthority,
    WriterIdentity,
    WriterLease,
    WriterLeaseError,
)

AUTHORIZATION_MATRIX = (
    ("pending_reconciliation", "cancel", True),
    ("pending_reconciliation", "cancel_all", True),
    ("pending_reconciliation", "submit", False),
    ("pending_reconciliation", "reduce_only", False),
    ("pending_reconciliation", "close", False),
    ("pending_reconciliation", "market", False),
    ("pending_reconciliation", "modify", False),
    ("cancel_only", "cancel", True),
    ("cancel_only", "cancel_all", True),
    ("cancel_only", "submit", False),
    ("cancel_only", "reduce_only", False),
    ("cancel_only", "close", False),
    ("cancel_only", "market", False),
    ("cancel_only", "modify", False),
    ("risk_increasing", "cancel", True),
    ("risk_increasing", "cancel_all", True),
    ("risk_increasing", "submit", True),
    ("risk_increasing", "reduce_only", True),
    ("risk_increasing", "close", True),
    ("risk_increasing", "market", True),
    ("risk_increasing", "modify", False),
)

HOLDER = """
import sys
from pathlib import Path
from execution.writer import WriterIdentity, WriterLease
decisions = []
lease = WriterLease.acquire(Path(sys.argv[1]), WriterIdentity(*sys.argv[2:]), decisions.append)
print(f"{lease.authority.mode}:{lease.authority.lease_epoch}", flush=True)
sys.stdin.readline(); lease.release()
"""


def _identity(instance="one", fingerprint="a" * 64):
    return WriterIdentity("hyperliquid:test-account", instance, fingerprint, "boot-one")


def _holder(root: Path, identity: WriterIdentity) -> subprocess.Popen[str]:
    fields = tuple(getattr(identity, name) for name in identity.__match_args__)
    process = subprocess.Popen(
        [sys.executable, "-B", "-u", "-c", HOLDER, str(root), *fields],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    ready, _, _ = select.select([process.stdout], [], [], 5)
    assert ready, process.stderr.read()
    assert process.poll() is None, process.stderr.read()
    return process


def _acquire(root: Path, identity: WriterIdentity):
    decisions = []
    return WriterLease.acquire(root, identity, decisions.append), decisions


def _lease_for_mode(root: Path, mode: str) -> WriterLease:
    identity = _identity()
    if mode == "cancel_only":
        authority = WriterAuthority(identity, mode, 1)
        path = WriterLease.path_for(root, identity.account_id)
        return WriterLease(path, authority, None, [].append, True)
    lease, _ = _acquire(root, identity)
    if mode == "risk_increasing":
        lease._authority = lease.authority._replace(mode=mode)
    return lease


def _shard_recorder(root: Path):
    writer = ShardWriter(root, boot_id="boot-one")
    sequence = 0

    def record(decision):
        nonlocal sequence
        sequence += 1
        writer.append_event(
            {
                "schema_ver": 1, "event_kind": "decision",
                "payload_schema": "writer_lease_decision", "venue": "hyperliquid",
                "conn_id": "writer-one", "boot_id": "boot-one",
                "recv_wall_ns": 1_000 + sequence, "recv_mono_ns": sequence,
                "source": "writer_lease", "seq_within_boot": sequence,
                "payload": decision._asdict(),
            }
        )

    return writer, record


def test_real_process_competition_release_and_crash_takeover(tmp_path: Path) -> None:
    assert AUTHORITY_MODES == frozenset(
        {"pending_reconciliation", "cancel_only", "risk_increasing"}
    )
    owner = _holder(tmp_path, _identity())
    assert owner.stdout.readline().strip() == "pending_reconciliation:1"
    path = WriterLease.path_for(tmp_path, _identity().account_id)
    assert "test-account" not in path.name and path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600

    observer, observer_events = _acquire(tmp_path, _identity("two", "b" * 64))
    assert observer.authority.mode == "cancel_only"
    assert observer.authorize("cancel") == observer.authority
    assert observer_events[-1].outcome == "cancel_only"
    denied = []
    with pytest.raises(WriterLeaseError, match="shared writer identity"):
        WriterLease.acquire(tmp_path, _identity("two"), denied.append)
    assert denied[-1].reason == "shared_writer_identity"
    with pytest.raises(WriterLeaseError, match="shared writer identity"):
        WriterLease.acquire(tmp_path, _identity("one", "b" * 64), denied.append)
    observer.release()
    assert len(observer_events) == 1
    owner.communicate("\n", timeout=5)
    assert owner.returncode == 0, owner.stderr.read()

    lease, _ = _acquire(tmp_path, _identity("three", "c" * 64))
    assert lease.authority.lease_epoch == 2
    lease.release()
    killed = _holder(tmp_path, _identity("four", "d" * 64))
    assert killed.stdout.readline().strip() == "pending_reconciliation:3"
    killed.kill()
    assert killed.wait(timeout=5) < 0
    takeover, _ = _acquire(tmp_path, _identity("five", "e" * 64))
    assert takeover.authority.lease_epoch == 4
    takeover.release()


@pytest.mark.parametrize(("mode", "action", "allowed"), AUTHORIZATION_MATRIX)
def test_writer_authorization_matrix(
    tmp_path: Path, mode: str, action: str, allowed: bool
) -> None:
    lease = _lease_for_mode(tmp_path, mode)
    if allowed:
        assert lease.authorize(action).mode == mode
    else:
        reason = "native_modify_disabled" if action == "modify" else "action not authorized"
        with pytest.raises(WriterLeaseError, match=reason):
            lease.authorize(action)
    lease.release()


def test_pending_and_cancel_only_have_the_same_allowed_actions() -> None:
    def allowed(mode):
        return {
            action for candidate, action, decision in AUTHORIZATION_MATRIX
            if candidate == mode and decision
        }

    assert allowed("pending_reconciliation") == allowed("cancel_only") == {"cancel", "cancel_all"}


@pytest.mark.parametrize(("action", "error"), (("typo", ValueError), (None, TypeError)))
def test_unknown_action_is_structurally_invalid_before_lease_revalidation(
    tmp_path: Path, action: object, error: type[Exception]
) -> None:
    lease, _ = _acquire(tmp_path, _identity())
    os.unlink(lease.path)
    lease.path.write_text("{}")
    with pytest.raises(error):
        lease.authorize(action)  # type: ignore[arg-type]
    with pytest.raises(WriterLeaseError, match="inode changed"):
        lease.authorize("cancel")


def test_symlink_and_replaced_inode_fail_closed(tmp_path: Path) -> None:
    path = WriterLease.path_for(tmp_path, _identity().account_id)
    (tmp_path / "target").write_text("not a lock")
    path.symlink_to(tmp_path / "target")
    denied = []
    with pytest.raises(WriterLeaseError, match="unsafe lock file"):
        WriterLease.acquire(tmp_path, _identity(), denied.append)
    assert denied[-1].reason == "unsafe_lock_file"

    path.unlink()
    lease, invalidated = _acquire(tmp_path, _identity())
    os.unlink(path)
    path.write_text("{}")
    with pytest.raises(WriterLeaseError, match="inode changed"):
        lease.revalidate()
    assert invalidated[-1].outcome == "invalidated"
    with pytest.raises(WriterLeaseError, match="no writer authority"):
        _ = lease.authority


def test_writer_decisions_append_to_the_real_durable_replay(tmp_path: Path) -> None:
    lock_root = tmp_path / "locks"
    lock_root.mkdir()
    writer, record = _shard_recorder(tmp_path / "events")
    lease = WriterLease.acquire(lock_root, _identity(), record)
    assert lease.revalidate() == lease.authority
    lease.release()
    writer.close()

    replay = replay_event_window(tmp_path / "events", 0, 2_000)
    assert [event["payload"]["action"] for event in replay.events] == ["acquire", "release"]
    acquired = replay.events[0]["payload"]
    assert acquired["account_digest"] == hashlib.sha256(_identity().account_id.encode()).hexdigest()
    path = WriterLease.path_for(lock_root, _identity().account_id)
    assert acquired["lock_path_digest"] == hashlib.sha256(str(path).encode()).hexdigest()
    assert acquired["wallet_fingerprint"] == "a" * 64 and not acquired["prior_epoch_valid"]


def test_acquire_and_deny_never_return_when_evidence_fails(tmp_path: Path) -> None:
    lock_root = tmp_path / "locks"
    lock_root.mkdir()
    closed, record = _shard_recorder(tmp_path / "closed")
    closed.close()
    with pytest.raises(WriterLeaseError, match="evidence"):
        WriterLease.acquire(lock_root, _identity(), record)

    owner, _ = _acquire(lock_root, _identity("owner", "b" * 64))
    assert owner.authority.lease_epoch == 2
    with pytest.raises(WriterLeaseError, match="evidence"):
        WriterLease.acquire(lock_root, _identity("observer", "c" * 64), record)
    owner.release()


def test_release_and_invalidation_preserve_primary_state_on_evidence_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock_root = tmp_path / "locks"
    lock_root.mkdir()
    writer, record = _shard_recorder(tmp_path / "release-events")
    lease = WriterLease.acquire(lock_root, _identity(), record)
    writer.close()
    lease.release()
    assert "writer evidence recording failed" in capsys.readouterr().err
    takeover, _ = _acquire(lock_root, _identity("takeover", "b" * 64))
    takeover.release()

    writer, record = _shard_recorder(tmp_path / "inode-events")
    lease = WriterLease.acquire(lock_root, _identity("inode", "c" * 64), record)
    writer.close()
    os.unlink(lease.path)
    lease.path.write_text("{}")
    with pytest.raises(WriterLeaseError, match="inode changed"):
        lease.revalidate()
    assert "writer evidence recording failed" in capsys.readouterr().err
