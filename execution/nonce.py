"""Local signer fencing for Hyperliquid nonce ownership."""

import fcntl
import hashlib
import os
import stat
from pathlib import Path


class SignerFenceError(RuntimeError):
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


def path_for(root: Path, wallet_fingerprint: str) -> Path:
    """Return a signer-only lock path without exposing the fingerprint."""
    _validate_fingerprint(wallet_fingerprint)
    base = Path(root).resolve(strict=True)
    digest = hashlib.sha256(wallet_fingerprint.encode()).hexdigest()
    return base / f"{digest}.signer.lock"


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
