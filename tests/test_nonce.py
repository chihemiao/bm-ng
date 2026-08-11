import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from data.contracts import PAYLOAD_SCHEMAS, ContractError, validate_envelope
from execution import nonce
from execution.nonce import SignerFence, SignerFenceError, path_for

FINGERPRINT = "a" * 64
ACCOUNT_DIGEST = "b" * 64
INSTANCE_ID = "writer-one"
DAY_MS = 86_400_000
NOW_MS = 5 * DAY_MS
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


def _nonce_event(**changes) -> dict:
    payload = {
        "wallet_fingerprint": FINGERPRINT,
        "account_digest": ACCOUNT_DIGEST,
        "instance_id": INSTANCE_ID,
        "allocated_nonce": NOW_MS,
        "previous_nonce": 1,
        "now_ms": NOW_MS,
        "outcome": "allocated",
        "reason": "nonce_allocated",
        "decided_ns": 1,
    }
    payload.update(changes)
    return {
        "schema_ver": 1, "event_kind": "decision",
        "payload_schema": "signer_nonce_allocation", "venue": "hyperliquid",
        "conn_id": "writer-one", "boot_id": "boot-one",
        "recv_wall_ns": 1, "recv_mono_ns": 1, "source": "nonce_allocator",
        "seq_within_boot": 1, "payload": payload,
    }


def test_replay_empty_or_frozen_nonce_rows_returns_zero() -> None:
    frozen = _nonce_event(outcome="frozen", reason="clock_backward", allocated_nonce=None)
    assert nonce.replay_last_allocated_nonce([], FINGERPRINT) == 0
    assert nonce.replay_last_allocated_nonce([frozen], FINGERPRINT) == 0


def test_replay_returns_largest_allocated_nonce_across_frozen_rows() -> None:
    events = [
        _nonce_event(allocated_nonce=10),
        _nonce_event(outcome="frozen", reason="clock_backward", allocated_nonce=None),
        _nonce_event(allocated_nonce=12),
    ]
    assert nonce.replay_last_allocated_nonce(events, FINGERPRINT) == 12


def test_replay_ignores_other_signers_but_not_malformed_matching_rows() -> None:
    other = _nonce_event(wallet_fingerprint="c" * 64, allocated_nonce=99)
    assert nonce.replay_last_allocated_nonce([other], FINGERPRINT) == 0
    malformed = _nonce_event(allocated_nonce=True)
    with pytest.raises(ValueError, match="allocated_nonce"):
        nonce.replay_last_allocated_nonce([malformed], FINGERPRINT)


def test_signer_nonce_schema_is_registered_durable_and_bounded() -> None:
    event = _nonce_event()
    assert validate_envelope(event) is event
    assert "signer_nonce_allocation" in PAYLOAD_SCHEMAS and len(PAYLOAD_SCHEMAS) == 19
    event.pop("seq_within_boot")
    with pytest.raises(ContractError, match="seq_within_boot"):
        validate_envelope(event)


@pytest.mark.parametrize("reason", ["clock_backward", "fence_invalidated"])
def test_frozen_nonce_decisions_have_no_allocated_value_or_time_relation(reason: str) -> None:
    event = _nonce_event(outcome="frozen", reason=reason, allocated_nonce=None, now_ms=1)
    assert validate_envelope(event)["payload"]["allocated_nonce"] is None


@pytest.mark.parametrize(
    ("allocated_nonce", "message"),
    [
        (NOW_MS - 2 * DAY_MS, "minus two days"),
        (NOW_MS - 2 * DAY_MS + 1, None),
        (NOW_MS + DAY_MS, "plus one day"),
        (NOW_MS + DAY_MS - 1, None),
    ],
)
def test_allocated_nonce_time_window_is_open(
    allocated_nonce: int, message: str | None,
) -> None:
    event = _nonce_event(allocated_nonce=allocated_nonce)
    if message is None:
        assert validate_envelope(event) is event
    else:
        with pytest.raises(ContractError, match=message):
            validate_envelope(event)


def test_clock_forward_is_not_an_allocatable_freeze_reason() -> None:
    with pytest.raises(ContractError, match="invalid nonce reason"):
        validate_envelope(
            _nonce_event(outcome="frozen", reason="clock_forward", allocated_nonce=None)
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"reason": "clock_backward"}, "allocated requires reason"),
        ({"outcome": "frozen", "reason": "nonce_allocated", "allocated_nonce": None},
         "reason nonce_allocated requires outcome"),
        ({"outcome": "frozen", "reason": "clock_backward"}, "must be null"),
        ({"allocated_nonce": None}, "positive integer"),
        ({"allocated_nonce": 1}, "exceed previous_nonce"),
        ({"allocated_nonce": 1, "previous_nonce": 2}, "exceed previous_nonce"),
    ],
)
def test_nonce_outcome_value_and_reason_form_a_closed_matrix(
    changes: dict, message: str,
) -> None:
    with pytest.raises(ContractError, match=message):
        validate_envelope(_nonce_event(**changes))
    assert validate_envelope(_nonce_event(previous_nonce=0))["payload"]["previous_nonce"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"wallet_fingerprint": "A" * 64}, {"account_digest": "account"},
        {"instance_id": ""}, {"allocated_nonce": True}, {"previous_nonce": -1},
        {"now_ms": 0}, {"outcome": "unknown"}, {"reason": "unknown"},
        {"decided_ns": 0}, {"extra": "field"},
    ],
)
def test_signer_nonce_format_rejects_invalid_or_extra_fields(changes: dict) -> None:
    with pytest.raises(ContractError):
        validate_envelope(_nonce_event(**changes))


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


def test_failed_contender_cannot_mutate_the_held_lock_file(tmp_path: Path) -> None:
    owner = _holder(tmp_path)
    path = path_for(tmp_path, FINGERPRINT)
    path.write_bytes(b"x")
    path.chmod(0o640)
    fixed_ns = 1_700_000_000_000_000_000
    os.utime(path, ns=(fixed_ns, fixed_ns))
    before = path.stat()
    with pytest.raises(SignerFenceError, match="signer fence contended"):
        SignerFence.acquire(tmp_path, FINGERPRINT, "contender")
    after = path.stat()
    owner.communicate("\n", timeout=5)
    assert (after.st_size, after.st_mode & 0o777, after.st_mtime_ns) == (
        before.st_size, before.st_mode & 0o777, before.st_mtime_ns,
    )
    takeover = SignerFence.acquire(tmp_path, FINGERPRINT, "takeover")
    assert path.stat().st_size == 0 and path.stat().st_mode & 0o777 == 0o600
    takeover.release()


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
