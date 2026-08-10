import fcntl
import hashlib
import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

AUTHORITY_MODES = frozenset({"pending_reconciliation", "cancel_only"})


class WriterLeaseError(RuntimeError):
    pass


class WriterIdentity(NamedTuple):
    account_id: str
    instance_id: str
    wallet_fingerprint: str
    boot_id: str


class WriterAuthority(NamedTuple):
    identity: WriterIdentity
    mode: str
    lease_epoch: int


class WriterLeaseDecision(NamedTuple):
    action: str
    outcome: str
    reason: str
    account_digest: str
    instance_id: str
    wallet_fingerprint: str
    boot_id: str
    lease_epoch: int | None
    lock_path_digest: str
    prior_epoch_valid: bool


Recorder = Callable[[WriterLeaseDecision], None]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise WriterLeaseError(message)


def _read_metadata(fd: int) -> dict:
    try:
        value = json.loads(os.pread(fd, 4096, 0) or b"{}")
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _same_inode(fd: int, path: Path) -> bool:
    opened, named = os.fstat(fd), os.stat(path, follow_symlinks=False)
    return (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _decision(
    identity: WriterIdentity, path: Path, action: str, outcome: str,
    reason: str, epoch: int | None, prior_epoch_valid: bool) -> WriterLeaseDecision:
    return WriterLeaseDecision(
        action, outcome, reason, _digest(identity.account_id), identity.instance_id,
        identity.wallet_fingerprint, identity.boot_id, epoch, _digest(str(path)), prior_epoch_valid)


def _record(recorder: Recorder, decision: WriterLeaseDecision, suppress: bool = False) -> None:
    try:
        recorder(decision)
    except Exception as exc:
        if not suppress:
            raise WriterLeaseError("writer evidence recording failed") from exc
        print("writer evidence recording failed", file=sys.stderr)


def _contended(
    fd: int, path: Path, identity: WriterIdentity, fingerprint: str,
    recorder: Recorder) -> WriterAuthority:
    metadata = _read_metadata(fd)
    os.close(fd)
    epoch = metadata.get("lease_epoch")
    prior = type(epoch) is int and epoch > 0
    same = metadata.get("wallet_fingerprint") == fingerprint
    same |= metadata.get("instance_id") == identity.instance_id
    if same:
        value = epoch if prior else None
        decision = _decision(
            identity, path, "deny", "terminated", "shared_writer_identity", value, prior
        )
        _record(recorder, decision)
        raise WriterLeaseError("shared writer identity") from None
    known = metadata.get("account_id") == identity.account_id and prior
    if not known:
        decision = _decision(
            identity, path, "deny", "terminated", "unknown_incumbent", None, prior
        )
        _record(recorder, decision)
        raise WriterLeaseError("unknown incumbent") from None
    decision = _decision(
        identity, path, "deny", "cancel_only", "incumbent_other_wallet", epoch, True
    )
    _record(recorder, decision)
    return WriterAuthority(identity, "cancel_only", epoch)


class WriterLease:
    def __init__(
        self, path: Path, authority: WriterAuthority, fd: int | None, recorder: Recorder,
        prior_epoch_valid: bool):
        self.path, self._authority, self._fd = path, authority, fd
        self._recorder, self._prior_epoch_valid = recorder, prior_epoch_valid

    @staticmethod
    def path_for(root: Path, account_id: str) -> Path:
        base = Path(root).resolve(strict=True)
        digest = hashlib.blake2s(account_id.encode(), digest_size=16).hexdigest()
        return base / f"{digest}.writer.lock"

    @classmethod
    def acquire(cls, root: Path, identity: WriterIdentity, recorder: Recorder) -> "WriterLease":
        _require(isinstance(identity, WriterIdentity), "invalid writer identity")
        _require(callable(recorder), "invalid writer recorder")
        metadata = identity._asdict()
        valid = all(isinstance(value, str) and value for value in metadata.values())
        fingerprint = metadata["wallet_fingerprint"]
        valid &= isinstance(fingerprint, str) and len(fingerprint) == 64
        valid &= all(char in "0123456789abcdef" for char in fingerprint)
        _require(valid, "invalid writer identity")
        path = cls.path_for(root, identity.account_id)
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            decision = _decision(
                identity, path, "deny", "terminated", "unsafe_lock_file", None, False
            )
            _record(recorder, decision)
            raise WriterLeaseError("unsafe lock file") from exc
        try:
            _require(stat.S_ISREG(os.fstat(fd).st_mode), "unsafe lock file")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _require(_same_inode(fd, path), "lock inode changed")
        except BlockingIOError:
            authority = _contended(fd, path, identity, fingerprint, recorder)
            return cls(path, authority, None, recorder, True)
        except (OSError, WriterLeaseError) as exc:
            os.close(fd)
            decision = _decision(
                identity, path, "deny", "terminated", "unsafe_lock_file", None, False
            )
            _record(recorder, decision)
            raise WriterLeaseError("unsafe lock file") from exc
        previous = _read_metadata(fd).get("lease_epoch")
        prior = type(previous) is int and previous >= 0
        epoch = previous + 1 if prior else 1
        metadata["lease_epoch"] = epoch
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
        os.ftruncate(fd, 0)
        os.pwrite(fd, encoded, 0)
        os.fsync(fd)
        authority = WriterAuthority(identity, "pending_reconciliation", epoch)
        decision = _decision(
            identity, path, "acquire", "pending_reconciliation", "lease_acquired", epoch, prior
        )
        try:
            _record(recorder, decision)
        except WriterLeaseError:
            os.close(fd)
            raise
        return cls(path, authority, fd, recorder, prior)

    @property
    def authority(self) -> WriterAuthority:
        _require(self._authority is not None, "no writer authority")
        return self._authority

    def revalidate(self) -> WriterAuthority:
        _require(self._fd is not None, "no held writer lease")
        authority = self.authority
        try:
            valid = _same_inode(self._fd, self.path)
        except OSError:
            valid = False
        if not valid:
            os.close(self._fd)
            self._fd = self._authority = None
            decision = _decision(
                authority.identity, self.path, "revalidate", "invalidated",
                "lock_inode_changed", authority.lease_epoch, self._prior_epoch_valid,
            )
            _record(self._recorder, decision, suppress=True)
            raise WriterLeaseError("lock inode changed")
        return authority

    def release(self) -> None:
        if self._fd is not None:
            authority = self.revalidate()
            os.close(self._fd)
            self._fd = self._authority = None
            decision = _decision(
                authority.identity, self.path, "release", "released",
                "lease_released", authority.lease_epoch, self._prior_epoch_valid,
            )
            _record(self._recorder, decision, suppress=True)
        self._fd = self._authority = None
