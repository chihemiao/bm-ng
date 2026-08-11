import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from execution.nonce import SignerFence, SignerFenceError, path_for

FINGERPRINT = "a" * 64
HOLDER = """
import sys
from pathlib import Path
from execution.nonce import SignerFence
fence = SignerFence.acquire(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
print("held", flush=True)
sys.stdin.readline(); fence.release()
"""


def _holder(root: Path, fingerprint: str = FINGERPRINT) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-B", "-u", "-c", HOLDER, str(root), fingerprint, "owner"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    ready, _, _ = select.select([process.stdout], [], [], 5)
    assert ready and process.stdout.readline().strip() == "held"
    assert process.poll() is None
    return process


@pytest.mark.parametrize(
    ("fingerprint", "instance_id", "error", "message"),
    [
        (None, "one", TypeError, "wallet_fingerprint must be str"),
        ("a" * 65, "one", ValueError, "wallet_fingerprint must be 64 lowercase hex"),
        ("A" * 64, "one", ValueError, "wallet_fingerprint must be 64 lowercase hex"),
        (FINGERPRINT, None, TypeError, "instance_id must be str"),
        (FINGERPRINT, "", ValueError, "instance_id must be nonempty"),
    ],
)
def test_invalid_signer_identity_precedes_filesystem_mutation(
    tmp_path: Path, fingerprint: object, instance_id: object,
    error: type[Exception], message: str,
) -> None:
    with pytest.raises(error, match=message):
        SignerFence.acquire(tmp_path, fingerprint, instance_id)  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


def test_distinct_signers_coexist_with_private_empty_lock_files(tmp_path: Path) -> None:
    first = SignerFence.acquire(tmp_path, FINGERPRINT, "one")
    second = SignerFence.acquire(tmp_path, "b" * 64, "two")
    assert first.wallet_fingerprint == FINGERPRINT and first.instance_id == "one"
    assert FINGERPRINT not in first.path.name and first.path == path_for(tmp_path, FINGERPRINT)
    assert first.path.stat().st_mode & 0o777 == 0o600
    assert first.path.stat().st_size == second.path.stat().st_size == 0
    first.release()
    first.release()
    second.release()


def test_real_process_contention_release_and_crash_takeover(tmp_path: Path) -> None:
    owner = _holder(tmp_path)
    with pytest.raises(SignerFenceError, match="signer fence contended"):
        SignerFence.acquire(tmp_path, FINGERPRINT, "contender")
    owner.communicate("\n", timeout=5)
    assert owner.returncode == 0
    takeover = SignerFence.acquire(tmp_path, FINGERPRINT, "takeover")
    takeover.release()

    crashed = _holder(tmp_path)
    crashed.kill()
    assert crashed.wait(timeout=5) < 0
    SignerFence.acquire(tmp_path, FINGERPRINT, "post-crash").release()


def test_symlink_and_replaced_inode_permanently_invalidate(tmp_path: Path) -> None:
    path = path_for(tmp_path, FINGERPRINT)
    target = tmp_path / "target"
    target.touch()
    path.symlink_to(target)
    with pytest.raises(SignerFenceError, match="signer lock path is not a regular file"):
        SignerFence.acquire(tmp_path, FINGERPRINT, "symlink")

    path.unlink()
    fence = SignerFence.acquire(tmp_path, FINGERPRINT, "inode")
    os.unlink(path)
    path.touch()
    with pytest.raises(SignerFenceError, match="signer lock inode changed"):
        fence.revalidate()
    with pytest.raises(SignerFenceError, match="signer fence invalidated"):
        fence.release()
