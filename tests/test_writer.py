import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from execution.writer import AUTHORITY_MODES, WriterIdentity, WriterLease, WriterLeaseError

HOLDER = """
import sys
from pathlib import Path
from execution.writer import WriterIdentity, WriterLease
lease = WriterLease.acquire(Path(sys.argv[1]), WriterIdentity(*sys.argv[2:]))
print(f"{lease.authority.mode}:{lease.authority.lease_epoch}", flush=True)
sys.stdin.readline(); lease.release()
"""


def _identity(instance="one", fingerprint="a" * 64):
    return WriterIdentity("hyperliquid:test-account", instance, fingerprint, "boot-one")


def _holder(root: Path, identity: WriterIdentity) -> subprocess.Popen[str]:
    fields = tuple(getattr(identity, name) for name in identity.__match_args__)
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", HOLDER, str(root), *fields],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    ready, _, _ = select.select([process.stdout], [], [], 5)
    assert ready, process.stderr.read()
    return process


def test_real_process_competition_release_and_crash_takeover(tmp_path: Path) -> None:
    assert AUTHORITY_MODES == frozenset({"pending_reconciliation", "cancel_only"})
    owner = _holder(tmp_path, _identity())
    assert owner.stdout.readline().strip() == "pending_reconciliation:1"
    path = WriterLease.path_for(tmp_path, _identity().account_id)
    assert "test-account" not in path.name and path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600

    observer = WriterLease.acquire(tmp_path, _identity("two", "b" * 64))
    assert observer.authority.mode == "cancel_only"
    with pytest.raises(WriterLeaseError, match="shared writer identity"):
        WriterLease.acquire(tmp_path, _identity("two"))
    with pytest.raises(WriterLeaseError, match="shared writer identity"):
        WriterLease.acquire(tmp_path, _identity("one", "b" * 64))
    observer.release()
    owner.communicate("\n", timeout=5)
    assert owner.returncode == 0, owner.stderr.read()

    lease = WriterLease.acquire(tmp_path, _identity("three", "c" * 64))
    assert lease.authority.lease_epoch == 2
    lease.release()
    killed = _holder(tmp_path, _identity("four", "d" * 64))
    assert killed.stdout.readline().strip() == "pending_reconciliation:3"
    killed.kill()
    assert killed.wait(timeout=5) < 0
    takeover = WriterLease.acquire(tmp_path, _identity("five", "e" * 64))
    assert takeover.authority.lease_epoch == 4
    takeover.release()


def test_symlink_and_replaced_inode_fail_closed(tmp_path: Path) -> None:
    path = WriterLease.path_for(tmp_path, _identity().account_id)
    (tmp_path / "target").write_text("not a lock")
    path.symlink_to(tmp_path / "target")
    with pytest.raises((OSError, WriterLeaseError)):
        WriterLease.acquire(tmp_path, _identity())

    path.unlink()
    lease = WriterLease.acquire(tmp_path, _identity())
    os.unlink(path)
    path.write_text("{}")
    with pytest.raises(WriterLeaseError, match="inode changed"):
        lease.revalidate()
    with pytest.raises(WriterLeaseError, match="no writer authority"):
        _ = lease.authority
