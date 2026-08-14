import fcntl
import hashlib
import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

AUTHORITY_MODES = frozenset(
    {"pending_reconciliation", "cancel_only", "flatten_only", "risk_increasing"}
)
WRITER_ACTIONS = frozenset(
    {"cancel", "cancel_all", "submit", "reduce_only", "close", "market", "modify"}
)
CANCEL_ACTIONS = frozenset({"cancel", "cancel_all"})


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


def _validate_demotion(demotion_ns: object, reason: object) -> None:
    if type(demotion_ns) is not int:
        raise TypeError("demotion_ns must be an integer")
    if demotion_ns <= 0:
        raise ValueError("demotion_ns must be positive")
    if type(reason) is not str:
        raise TypeError("demotion reason must be text")
    if not reason.startswith("writer_demoted:") or reason == "writer_demoted:":
        raise ValueError("invalid demotion reason")


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
        prior_epoch_valid: bool, *, acquired_ns: int | None):
        self.path, self._authority, self._fd = path, authority, fd
        self._recorder, self._prior_epoch_valid = recorder, prior_epoch_valid
        self._acquired_ns = acquired_ns

    @staticmethod
    def path_for(root: Path, account_id: str) -> Path:
        base = Path(root).resolve(strict=True)
        digest = hashlib.blake2s(account_id.encode(), digest_size=16).hexdigest()
        return base / f"{digest}.writer.lock"

    @classmethod
    def acquire(
        cls, root: Path, identity: WriterIdentity, recorder: Recorder, *, acquired_ns: int
    ) -> "WriterLease":
        if type(acquired_ns) is not int:
            raise TypeError("acquired_ns must be an integer")
        if acquired_ns <= 0:
            raise ValueError("acquired_ns must be positive")
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
            return cls(path, authority, None, recorder, True, acquired_ns=None)
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
        return cls(path, authority, fd, recorder, prior, acquired_ns=acquired_ns)

    @property
    def authority(self) -> WriterAuthority:
        _require(self._authority is not None, "no writer authority")
        return self._authority

    def revalidate(self) -> WriterAuthority:
        authority = self.authority
        if authority.mode == "cancel_only":
            return authority
        _require(self._fd is not None, "no held writer lease")
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

    def authorize(self, action: str) -> WriterAuthority:
        if not isinstance(action, str):
            raise TypeError("writer action must be text")
        if action not in WRITER_ACTIONS:
            raise ValueError("unknown writer action")
        authority = self.revalidate()
        cause = None
        message = None
        if action == "modify":
            cause = message = "native_modify_disabled"
        if authority.mode == "risk_increasing":
            allowed = WRITER_ACTIONS
        elif authority.mode == "flatten_only":
            allowed = CANCEL_ACTIONS | {"reduce_only"}
        else:
            allowed = CANCEL_ACTIONS
        if action not in allowed and cause is None:
            cause = "action_not_authorized"
            message = "writer action not authorized"
        if cause is None:
            return authority
        reason = f"authorize_denied:{authority.mode}:{action}:{cause}"
        decision = _decision(
            authority.identity, self.path, "authorize", "denied", reason,
            authority.lease_epoch, self._prior_epoch_valid,
        )
        try:
            self._recorder(decision)
        except Exception as exc:
            raise WriterLeaseError(message) from exc
        raise WriterLeaseError(message)

    def elevate_to_risk_increasing(
        self, *, promotion_ns: int,
        before_elevate: Callable[[WriterAuthority], None],
    ) -> WriterAuthority:
        if type(promotion_ns) is not int:
            raise TypeError("promotion_ns must be an integer")
        if promotion_ns <= 0:
            raise ValueError("promotion_ns must be positive")
        if not callable(before_elevate):
            raise TypeError("before_elevate must be callable")
        authority = self.revalidate()
        _require(authority.mode == "pending_reconciliation", "writer lease not promotable")
        _require(self._acquired_ns is not None, "missing writer acquisition time")
        if promotion_ns < self._acquired_ns:
            raise ValueError("promotion_ns predates acquisition")
        before_elevate(authority)
        elevated = authority._replace(mode="risk_increasing")
        self._authority = elevated
        return elevated

    def demote_to_cancel_only(self, *, demotion_ns: int, reason: str) -> WriterAuthority:
        _validate_demotion(demotion_ns, reason)
        authority = self.authority
        if authority.mode not in {"flatten_only", "risk_increasing"}:
            return authority
        authority = self.revalidate()
        demoted = authority._replace(mode="cancel_only")
        self._authority = demoted
        decision = _decision(
            authority.identity, self.path, "demote", "cancel_only", reason,
            authority.lease_epoch, self._prior_epoch_valid,
        )
        try:
            self._recorder(decision)
        except Exception as exc:
            message = "writer demotion applied; evidence recording failed"
            raise WriterLeaseError(message) from exc
        return demoted

    def demote_to_flatten_only(self, *, demotion_ns: int, reason: str) -> WriterAuthority:
        _validate_demotion(demotion_ns, reason)
        authority = self.revalidate()
        _require(authority.mode == "risk_increasing", "writer lease not risk increasing")
        demoted = authority._replace(mode="flatten_only")
        self._authority = demoted
        decision = _decision(
            authority.identity, self.path, "demote", "flatten_only", reason,
            authority.lease_epoch, self._prior_epoch_valid,
        )
        try:
            self._recorder(decision)
        except Exception as exc:
            message = "writer flatten restriction applied; evidence recording failed"
            raise WriterLeaseError(message) from exc
        return demoted

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


def _writer_metadata(root: Path, account_id: str, suffix: str) -> dict:
    if type(account_id) is not str:
        raise TypeError("account_id must be text")
    if not account_id:
        raise ValueError("account_id must not be empty")
    try:
        path = WriterLease.path_for(root, account_id).with_suffix(suffix)
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        return {}
    try:
        safe = stat.S_ISREG((mode := os.fstat(fd).st_mode)) and stat.S_IMODE(mode) == 0o600
        return _read_metadata(fd) if safe else {}
    finally:
        os.close(fd)


def read_current_epoch(root: Path, account_id: str) -> tuple[str, int] | None:
    metadata = _writer_metadata(root, account_id, ".lock")
    epoch = metadata.get("lease_epoch")
    if metadata.get("account_id") == account_id and type(epoch) is int and epoch > 0:
        return _digest(account_id), epoch
    return None


def read_heartbeat(root: Path, account_id: str) -> tuple[str, int, int] | None:
    metadata = _writer_metadata(root, account_id, ".heartbeat")
    digest, epoch, observed = (
        metadata.get("account_digest"), metadata.get("lease_epoch"),
        metadata.get("observed_mono_ns"),
    )
    valid = set(metadata) == {"account_digest", "lease_epoch", "observed_mono_ns"}
    valid = valid and isinstance(digest, str) and len(digest) == 64
    valid = valid and all(char in "0123456789abcdef" for char in digest)
    valid = valid and type(epoch) is int and epoch > 0
    valid = valid and type(observed) is int and observed > 0
    return (digest, epoch, observed) if valid else None


def publish_heartbeat(lease: WriterLease, *, observed_mono_ns: int) -> None:
    if not isinstance(lease, WriterLease):
        raise TypeError("lease must be WriterLease")
    if type(observed_mono_ns) is not int:
        raise TypeError("observed_mono_ns must be an integer")
    if observed_mono_ns <= 0:
        raise ValueError("observed_mono_ns must be positive")
    authority = lease.revalidate()
    _require(
        authority.mode != "cancel_only" and lease._fd is not None,
        "heartbeat requires held primary lease",
    )
    payload = {"account_digest": _digest(authority.identity.account_id),
               "lease_epoch": authority.lease_epoch, "observed_mono_ns": observed_mono_ns}
    path, temporary = lease.path.with_suffix(".heartbeat"), lease.path.with_suffix(".heartbeat.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        _require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "unsafe heartbeat file")
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    os.replace(temporary, path)
