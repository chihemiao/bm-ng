import fcntl
import hashlib
import json
import os
import stat
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


class WriterLease:
    def __init__(self, path: Path, authority: WriterAuthority, fd: int | None):
        self.path, self._authority, self._fd = path, authority, fd

    @staticmethod
    def path_for(root: Path, account_id: str) -> Path:
        base = Path(root).resolve(strict=True)
        digest = hashlib.blake2s(account_id.encode(), digest_size=16).hexdigest()
        return base / f"{digest}.writer.lock"

    @classmethod
    def acquire(cls, root: Path, identity: WriterIdentity) -> "WriterLease":
        _require(isinstance(identity, WriterIdentity), "invalid writer identity")
        metadata = identity._asdict()
        valid = all(isinstance(value, str) and value for value in metadata.values())
        fingerprint = metadata["wallet_fingerprint"]
        valid &= isinstance(fingerprint, str) and len(fingerprint) == 64
        valid &= all(char in "0123456789abcdef" for char in fingerprint)
        _require(valid, "invalid writer identity")
        path = cls.path_for(root, identity.account_id)
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(path, flags, 0o600)
        try:
            _require(stat.S_ISREG(os.fstat(fd).st_mode), "unsafe lock file")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _require(_same_inode(fd, path), "lock inode changed")
        except BlockingIOError:
            metadata = _read_metadata(fd)
            os.close(fd)
            same = metadata.get("wallet_fingerprint") == fingerprint
            same |= metadata.get("instance_id") == identity.instance_id
            _require(not same, "shared writer identity")
            epoch = metadata.get("lease_epoch")
            _require(metadata.get("account_id") == identity.account_id, "unknown incumbent")
            _require(isinstance(epoch, int) and epoch > 0, "incumbent identity unavailable")
            authority = WriterAuthority(identity, "cancel_only", epoch)
            return cls(path, authority, None)
        except (OSError, WriterLeaseError) as exc:
            os.close(fd)
            raise WriterLeaseError("unsafe lock file") from exc
        previous = _read_metadata(fd).get("lease_epoch", 0)
        epoch = previous + 1 if isinstance(previous, int) and previous >= 0 else 1
        metadata["lease_epoch"] = epoch
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
        os.ftruncate(fd, 0)
        os.pwrite(fd, encoded, 0)
        os.fsync(fd)
        authority = WriterAuthority(identity, "pending_reconciliation", epoch)
        return cls(path, authority, fd)

    @property
    def authority(self) -> WriterAuthority:
        _require(self._authority is not None, "no writer authority")
        return self._authority

    def revalidate(self) -> WriterAuthority:
        _require(self._fd is not None, "no held writer lease")
        try:
            valid = _same_inode(self._fd, self.path)
        except OSError:
            valid = False
        if not valid:
            os.close(self._fd)
            self._fd = self._authority = None
            raise WriterLeaseError("lock inode changed")
        return self.authority

    def release(self) -> None:
        if self._fd is not None:
            self.revalidate()
            os.close(self._fd)
        self._fd = self._authority = None
