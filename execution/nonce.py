"""Local signer fencing for Hyperliquid nonce ownership."""

import fcntl
import hashlib
import os
import stat
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from data.schema_nonce import DAY_MS

NONCE_EVENT_SCHEMA = "signer_nonce_allocation"
FROZEN_REASONS = frozenset({"clock_backward", "fence_invalidated"})


class SignerFenceError(RuntimeError):
    pass


class NonceAllocationError(RuntimeError):
    pass


def _validate_fingerprint(wallet_fingerprint: str) -> None:
    if not isinstance(wallet_fingerprint, str):
        raise TypeError("wallet_fingerprint must be str")
    is_hex = all(char in "0123456789abcdef" for char in wallet_fingerprint)
    if len(wallet_fingerprint) != 64 or not is_hex:
        raise ValueError("wallet_fingerprint must be 64 lowercase hex")


def _validate_instance(instance_id: str) -> None:
    if not isinstance(instance_id, str):
        raise TypeError("instance_id must be str")
    if not instance_id:
        raise ValueError("instance_id must be nonempty")


def _validate_account_digest(account_digest: str) -> None:
    if not isinstance(account_digest, str):
        raise TypeError("account_digest must be str")
    is_hex = all(char in "0123456789abcdef" for char in account_digest)
    if len(account_digest) != 64 or not is_hex:
        raise ValueError("account_digest must be 64 lowercase hex")


def _validate_positive(value: int, name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be int")
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def path_for(root: Path, wallet_fingerprint: str) -> Path:
    """Return a signer-only lock path without exposing the fingerprint."""
    _validate_fingerprint(wallet_fingerprint)
    base = Path(root).resolve(strict=True)
    digest = hashlib.sha256(wallet_fingerprint.encode()).hexdigest()
    return base / f"{digest}.signer.lock"


def replay_last_allocated_nonce(
    events: Iterable[Mapping[str, object]], wallet_fingerprint: str,
) -> int:
    """Replay the durable maximum for one signer without revalidating envelopes."""
    _validate_fingerprint(wallet_fingerprint)
    last = 0
    for event in events:
        if event.get("payload_schema") != NONCE_EVENT_SCHEMA:
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("signer nonce payload must be a mapping")
        if payload.get("wallet_fingerprint") != wallet_fingerprint:
            continue
        if payload.get("outcome") != "allocated":
            continue
        allocated = payload.get("allocated_nonce")
        if type(allocated) is not int:
            raise ValueError("allocated_nonce must be int")
        last = max(last, allocated)
    return last


def replay_freeze_reason(
    events: Iterable[Mapping[str, object]], wallet_fingerprint: str,
) -> str | None:
    """Return one durable signer freeze, rejecting duplicate or invalid rows."""
    _validate_fingerprint(wallet_fingerprint)
    reason = None
    for event in events:
        if event.get("payload_schema") != NONCE_EVENT_SCHEMA:
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("signer nonce payload must be a mapping")
        if payload.get("wallet_fingerprint") != wallet_fingerprint:
            continue
        if payload.get("outcome") != "frozen":
            continue
        found = payload.get("reason")
        if not isinstance(found, str) or found not in FROZEN_REASONS:
            raise ValueError("invalid freeze reason")
        if reason is not None:
            raise ValueError("multiple signer nonce freeze rows")
        reason = found
    return reason


class SignerFence:
    def __init__(self, path: Path, wallet_fingerprint: str, instance_id: str, fd: int):
        self.path = path
        self.wallet_fingerprint = wallet_fingerprint
        self.instance_id = instance_id
        self._fd: int | None = fd
        self._invalidated = False

    @classmethod
    def acquire(
        cls, root: Path, wallet_fingerprint: str, instance_id: str,
    ) -> "SignerFence":
        _validate_instance(instance_id)
        path = path_for(root, wallet_fingerprint)
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise SignerFenceError("signer lock path is not a regular file") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SignerFenceError("signer lock path is not a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise SignerFenceError("signer fence contended") from exc
        except (OSError, SignerFenceError) as exc:
            os.close(fd)
            raise SignerFenceError("signer lock path is not a regular file") from exc
        try:
            os.fchmod(fd, 0o600)
            os.ftruncate(fd, 0)
        except OSError as exc:
            os.close(fd)
            raise SignerFenceError("signer lock setup failed") from exc
        fence = cls(path, wallet_fingerprint, instance_id, fd)
        fence.revalidate()
        return fence

    def revalidate(self) -> None:
        if self._invalidated or self._fd is None:
            raise SignerFenceError("signer fence invalidated")
        try:
            opened = os.fstat(self._fd)
            named = os.stat(self.path, follow_symlinks=False)
            valid = (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)
        except OSError:
            valid = False
        if not valid:
            os.close(self._fd)
            self._fd = None
            self._invalidated = True
            raise SignerFenceError("signer lock inode changed")

    def release(self) -> None:
        if self._invalidated:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            raise SignerFenceError("signer fence invalidated")
        if self._fd is None:
            return
        self.revalidate()
        os.close(self._fd)
        self._fd = None


class NonceAllocator:
    def __init__(
        self, fence: SignerFence, *, account_digest: str, instance_id: str,
        replayed_last: int, recorder: Callable[[Mapping[str, object]], None],
    ) -> None:
        if not isinstance(fence, SignerFence):
            raise TypeError("fence must be SignerFence")
        _validate_account_digest(account_digest)
        _validate_instance(instance_id)
        if type(replayed_last) is not int:
            raise TypeError("replayed_last must be int")
        if replayed_last < 0:
            raise ValueError("replayed_last must be nonnegative")
        if not callable(recorder):
            raise TypeError("recorder must be callable")
        self._fence = fence
        self._account_digest = account_digest
        self._instance_id = instance_id
        self._last = replayed_last
        self._recorder = recorder

    @property
    def last_nonce(self) -> int:
        return self._last

    def allocate(self, *, now_ms: int, decided_ns: int) -> int:
        _validate_positive(now_ms, "now_ms")
        _validate_positive(decided_ns, "decided_ns")
        self._fence.revalidate()
        candidate = max(self._last, now_ms) + 1
        if candidate >= now_ms + DAY_MS:
            raise NonceAllocationError("candidate nonce exceeds Hyperliquid time window")
        assert candidate > now_ms - 2 * DAY_MS  # Unreachable lower bound by construction.
        payload = {
            "wallet_fingerprint": self._fence.wallet_fingerprint,
            "account_digest": self._account_digest,
            "instance_id": self._instance_id,
            "allocated_nonce": candidate,
            "previous_nonce": self._last,
            "now_ms": now_ms,
            "outcome": "allocated",
            "reason": "nonce_allocated",
            "decided_ns": decided_ns,
        }
        self._recorder(payload)
        self._last = candidate
        return candidate
