import hashlib
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from data.contracts import ContractError, validate_envelope
from data.shard import ShardWriter, replay_event_window
from execution.writer import (
    AUTHORITY_MODES,
    WriterAuthority,
    WriterIdentity,
    WriterLease,
    WriterLeaseError,
)

AUTHORIZATION_MATRIX = (
    ("pending_reconciliation", "cancel", True, None),
    ("pending_reconciliation", "cancel_all", True, None),
    ("pending_reconciliation", "submit", False, "action_not_authorized"),
    ("pending_reconciliation", "reduce_only", False, "action_not_authorized"),
    ("pending_reconciliation", "close", False, "action_not_authorized"),
    ("pending_reconciliation", "market", False, "action_not_authorized"),
    ("pending_reconciliation", "modify", False, "native_modify_disabled"),
    ("cancel_only", "cancel", True, None),
    ("cancel_only", "cancel_all", True, None),
    ("cancel_only", "submit", False, "action_not_authorized"),
    ("cancel_only", "reduce_only", False, "action_not_authorized"),
    ("cancel_only", "close", False, "action_not_authorized"),
    ("cancel_only", "market", False, "action_not_authorized"),
    ("cancel_only", "modify", False, "native_modify_disabled"),
    ("flatten_only", "cancel", True, None),
    ("flatten_only", "cancel_all", True, None),
    ("flatten_only", "submit", False, "action_not_authorized"),
    ("flatten_only", "reduce_only", True, None),
    ("flatten_only", "close", False, "action_not_authorized"),
    ("flatten_only", "market", False, "action_not_authorized"),
    ("flatten_only", "modify", False, "native_modify_disabled"),
    ("risk_increasing", "cancel", True, None),
    ("risk_increasing", "cancel_all", True, None),
    ("risk_increasing", "submit", True, None),
    ("risk_increasing", "reduce_only", True, None),
    ("risk_increasing", "close", True, None),
    ("risk_increasing", "market", True, None),
    ("risk_increasing", "modify", False, "native_modify_disabled"),
)

HOLDER = """
import sys
from pathlib import Path
from execution.writer import WriterIdentity, WriterLease
decisions = []
lease = WriterLease.acquire(
    Path(sys.argv[1]), WriterIdentity(*sys.argv[2:]), decisions.append, acquired_ns=100
)
print(f"{lease.authority.mode}:{lease.authority.lease_epoch}", flush=True)
sys.stdin.readline(); lease.release()
"""


def _identity(instance="one", fingerprint="a" * 64):
    return WriterIdentity("hyperliquid:test-account", instance, fingerprint, "boot-one")


def _promotion_event(**changes) -> dict:
    payload = {
        "account_digest": "a" * 64, "instance_id": "writer-one",
        "boot_id": "identity-boot", "lease_epoch": 1,
        "from_mode": "pending_reconciliation", "to_mode": "risk_increasing",
        "outcome": "promoted", "reason": "admission_ready",
        "admission_action": "ready", "admission_digest": "b" * 64,
        "decided_ns": 900,
    }
    payload.update(changes)
    return {
        "schema_ver": 1, "event_kind": "decision",
        "payload_schema": "writer_authority_promotion", "venue": "hyperliquid",
        "conn_id": "writer-one", "boot_id": "boot-one",
        "recv_wall_ns": 1_000, "recv_mono_ns": 900,
        "source": "writer_promotion", "seq_within_boot": 10, "payload": payload,
    }


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
    return WriterLease.acquire(
        root, identity, decisions.append, acquired_ns=100
    ), decisions


def _lease_for_mode(root: Path, mode: str, recorder) -> WriterLease:
    identity = _identity()
    if mode == "cancel_only":
        authority = WriterAuthority(identity, mode, 1)
        path = WriterLease.path_for(root, identity.account_id)
        return WriterLease(path, authority, None, recorder, True, acquired_ns=None)
    lease = WriterLease.acquire(root, identity, recorder, acquired_ns=100)
    if mode in {"flatten_only", "risk_increasing"}:
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


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {
            "outcome": "denied", "to_mode": "pending_reconciliation",
            "reason": "admission_freeze", "admission_action": "cancel_only_freeze",
        },
        {
            "outcome": "denied", "from_mode": "cancel_only", "to_mode": "cancel_only",
            "reason": "not_promotable_mode", "admission_action": "ready",
        },
        {
            "outcome": "denied", "from_mode": "cancel_only", "to_mode": "cancel_only",
            "reason": "not_promotable_mode", "admission_action": "cancel_only_freeze",
        },
    ],
)
def test_writer_authority_promotion_has_a_closed_decision_matrix(changes: dict) -> None:
    assert validate_envelope(_promotion_event(**changes))["payload"]


def test_writer_authority_promotion_rejects_invalid_fields_and_combinations() -> None:
    invalid = [
        _promotion_event(to_mode="pending_reconciliation"),
        _promotion_event(account_digest="raw-account"),
        _promotion_event(admission_digest="raw-admission"),
        _promotion_event(lease_epoch=0),
        _promotion_event(decided_ns=0),
    ]
    missing = _promotion_event()
    missing["payload"].pop("admission_action")
    invalid.append(missing)
    for event in invalid:
        with pytest.raises(ContractError):
            validate_envelope(event)
    wrong_kind = _promotion_event()
    wrong_kind["event_kind"] = "ops"
    with pytest.raises(ContractError, match="event kind"):
        validate_envelope(wrong_kind)


def test_real_process_competition_release_and_crash_takeover(tmp_path: Path) -> None:
    assert AUTHORITY_MODES == frozenset(
        {"pending_reconciliation", "cancel_only", "flatten_only", "risk_increasing"}
    )
    owner = _holder(tmp_path, _identity())
    assert owner.stdout.readline().strip() == "pending_reconciliation:1"
    path = WriterLease.path_for(tmp_path, _identity().account_id)
    assert "test-account" not in path.name and path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600

    observer, observer_events = _acquire(tmp_path, _identity("two", "b" * 64))
    assert observer.authority.mode == "cancel_only" != "flatten_only"
    assert observer.authorize("cancel") == observer.authority
    assert observer_events[-1].outcome == "cancel_only"
    denied = []
    with pytest.raises(WriterLeaseError, match="shared writer identity"):
        WriterLease.acquire(tmp_path, _identity("two"), denied.append, acquired_ns=100)
    assert denied[-1].reason == "shared_writer_identity"
    with pytest.raises(WriterLeaseError, match="shared writer identity"):
        WriterLease.acquire(
            tmp_path, _identity("one", "b" * 64), denied.append, acquired_ns=100
        )
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


@pytest.mark.parametrize(("mode", "action", "allowed", "cause"), AUTHORIZATION_MATRIX)
def test_writer_authorization_matrix(
    tmp_path: Path, mode: str, action: str, allowed: bool, cause: str | None
) -> None:
    decisions = []
    lease = _lease_for_mode(tmp_path, mode, decisions.append)
    before = len(decisions)
    if allowed:
        assert lease.authorize(action).mode == mode
        assert len(decisions) == before
    else:
        reason = "native_modify_disabled" if action == "modify" else "action not authorized"
        with pytest.raises(WriterLeaseError, match=reason):
            lease.authorize(action)
        assert len(decisions) == before + 1
        decision = decisions[-1]
        assert (decision.action, decision.outcome) == ("authorize", "denied")
        assert decision.reason == f"authorize_denied:{mode}:{action}:{cause}"
    lease.release()


def test_pending_and_cancel_only_have_the_same_allowed_actions() -> None:
    def allowed(mode):
        return {
            action for candidate, action, decision, _ in AUTHORIZATION_MATRIX
            if candidate == mode and decision
        }

    assert allowed("pending_reconciliation") == allowed("cancel_only") == {"cancel", "cancel_all"}


@pytest.mark.parametrize(("action", "error"), (("typo", ValueError), (None, TypeError)))
def test_unknown_action_is_structurally_invalid_before_lease_revalidation(
    tmp_path: Path, action: object, error: type[Exception]
) -> None:
    lease, decisions = _acquire(tmp_path, _identity())
    os.unlink(lease.path)
    lease.path.write_text("{}")
    before = len(decisions)
    with pytest.raises(error):
        lease.authorize(action)  # type: ignore[arg-type]
    assert len(decisions) == before
    with pytest.raises(WriterLeaseError, match="inode changed"):
        lease.authorize("cancel")


def test_authorization_denial_preserves_reason_when_recording_fails(tmp_path: Path) -> None:
    failure = OSError("decision stream unavailable")

    def fail(_decision) -> None:
        raise failure

    identity = _identity()
    authority = WriterAuthority(identity, "cancel_only", 1)
    path = WriterLease.path_for(tmp_path, identity.account_id)
    lease = WriterLease(path, authority, None, fail, True, acquired_ns=None)
    with pytest.raises(WriterLeaseError) as caught:
        lease.authorize("submit")
    assert str(caught.value) == "writer action not authorized"
    assert caught.value.__cause__ is failure


def test_symlink_and_replaced_inode_fail_closed(tmp_path: Path) -> None:
    path = WriterLease.path_for(tmp_path, _identity().account_id)
    (tmp_path / "target").write_text("not a lock")
    path.symlink_to(tmp_path / "target")
    denied = []
    with pytest.raises(WriterLeaseError, match="unsafe lock file"):
        WriterLease.acquire(tmp_path, _identity(), denied.append, acquired_ns=100)
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
    lease = WriterLease.acquire(lock_root, _identity(), record, acquired_ns=100)
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
        WriterLease.acquire(lock_root, _identity(), record, acquired_ns=100)

    owner, _ = _acquire(lock_root, _identity("owner", "b" * 64))
    assert owner.authority.lease_epoch == 2
    with pytest.raises(WriterLeaseError, match="evidence"):
        WriterLease.acquire(
            lock_root, _identity("observer", "c" * 64), record, acquired_ns=100
        )
    owner.release()


def test_release_and_invalidation_preserve_primary_state_on_evidence_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock_root = tmp_path / "locks"
    lock_root.mkdir()
    writer, record = _shard_recorder(tmp_path / "release-events")
    lease = WriterLease.acquire(lock_root, _identity(), record, acquired_ns=100)
    writer.close()
    lease.release()
    assert "writer evidence recording failed" in capsys.readouterr().err
    takeover, _ = _acquire(lock_root, _identity("takeover", "b" * 64))
    takeover.release()

    writer, record = _shard_recorder(tmp_path / "inode-events")
    lease = WriterLease.acquire(
        lock_root, _identity("inode", "c" * 64), record, acquired_ns=100
    )
    writer.close()
    os.unlink(lease.path)
    lease.path.write_text("{}")
    with pytest.raises(WriterLeaseError, match="inode changed"):
        lease.revalidate()
    assert "writer evidence recording failed" in capsys.readouterr().err
