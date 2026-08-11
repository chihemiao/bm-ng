import hashlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Literal

from data.contracts import ROTATION_LEAD_NS, VALIDITY_NS
from data.schema_wallet import wallet_rotation_semantic_errors
from execution.writer import WriterIdentity, WriterLease, WriterLeaseError

WalletAssessment = Literal["active", "rotation_due", "expired"]


@dataclass(frozen=True)
class AgentWalletRegistration:
    wallet_fingerprint: str
    issued_ns: int
    expires_ns: int

    def __post_init__(self) -> None:
        fingerprint = self.wallet_fingerprint
        valid_fingerprint = isinstance(fingerprint, str) and len(fingerprint) == 64
        valid_fingerprint &= all(char in "0123456789abcdef" for char in fingerprint)
        if not valid_fingerprint:
            raise ValueError("wallet_fingerprint must be 64 lowercase hex characters")
        if type(self.issued_ns) is not int:
            raise TypeError("issued_ns must be an integer")
        if self.issued_ns <= 0:
            raise ValueError("issued_ns must be positive")
        if type(self.expires_ns) is not int:
            raise TypeError("expires_ns must be an integer")
        if self.expires_ns != self.issued_ns + VALIDITY_NS:
            raise ValueError("expires_ns must equal issued_ns plus validity")


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def _decision_require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True, slots=True)
class AgentWalletRotationDecision:
    account_digest: str
    instance_id: str
    boot_id: str
    old_wallet_fingerprint: str
    new_wallet_fingerprint: str
    old_issued_ns: int
    old_expires_ns: int
    new_issued_ns: int
    new_expires_ns: int
    assessment: str
    outcome: str
    reason: str
    decided_ns: int

    def __post_init__(self) -> None:
        for name in (
            "account_digest", "old_wallet_fingerprint", "new_wallet_fingerprint"
        ):
            _decision_require(_valid_digest(getattr(self, name)), f"invalid {name}")
        for name in ("instance_id", "boot_id"):
            value = getattr(self, name)
            _decision_require(isinstance(value, str) and bool(value), f"invalid {name}")
        for name in (
            "old_issued_ns", "old_expires_ns", "new_issued_ns", "new_expires_ns",
            "decided_ns",
        ):
            value = getattr(self, name)
            _decision_require(type(value) is int and value > 0, f"invalid {name}")
        errors = wallet_rotation_semantic_errors(
            asdict(self), validity_ns=VALIDITY_NS, rotation_lead_ns=ROTATION_LEAD_NS
        )
        _decision_require(not errors, errors[0] if errors else "")


class RotationRecordError(RuntimeError):
    def __init__(self, lease: WriterLease, cause: BaseException):
        super().__init__("wallet rotation evidence recording failed")
        self.lease, self.cause = lease, cause


def _rotation_decision(
    identity: WriterIdentity, old: AgentWalletRegistration, new: AgentWalletRegistration,
    assessment: WalletAssessment,
    outcome: str, reason: str, now_ns: int,
) -> AgentWalletRotationDecision:
    return AgentWalletRotationDecision(
        hashlib.sha256(identity.account_id.encode()).hexdigest(),
        identity.instance_id, identity.boot_id,
        old.wallet_fingerprint, new.wallet_fingerprint,
        old.issued_ns, old.expires_ns, new.issued_ns, new.expires_ns,
        assessment, outcome, reason, now_ns,
    )


def _validate_rotation_args(
    lease: object, old: object, new: object,
    recorder: object, reacquire: object, now_ns: object,
) -> None:
    if not isinstance(lease, WriterLease):
        raise TypeError("lease must be a WriterLease")
    if not isinstance(old, AgentWalletRegistration):
        raise TypeError("old_registration must be an AgentWalletRegistration")
    if not isinstance(new, AgentWalletRegistration):
        raise TypeError("new_registration must be an AgentWalletRegistration")
    if not all((callable(recorder), callable(reacquire))):
        raise TypeError("rotation callbacks must be callable")
    if type(now_ns) is not int:
        raise TypeError("now_ns must be an integer")
    if now_ns <= 0:
        raise ValueError("now_ns must be positive")


def _record_abort(
    recorder: Callable[[AgentWalletRotationDecision], None], identity: WriterIdentity,
    old: AgentWalletRegistration, new: AgentWalletRegistration,
    assessment: WalletAssessment, reason: str, now_ns: int,
) -> None:
    recorder(_rotation_decision(
        identity, old, new, assessment, "aborted", reason, now_ns))


def _record_lease_abort(
    recorder: Callable[[AgentWalletRotationDecision], None], lease: WriterLease,
    identity: WriterIdentity, old: AgentWalletRegistration,
    new: AgentWalletRegistration, assessment: WalletAssessment, reason: str, now_ns: int,
) -> None:
    try:
        _record_abort(recorder, identity, old, new, assessment, reason, now_ns)
    except Exception as exc:
        raise RotationRecordError(lease, exc) from exc


def rotate_agent_wallet(lease: WriterLease, old_registration: AgentWalletRegistration,
    new_registration: AgentWalletRegistration,
    recorder: Callable[[AgentWalletRotationDecision], None],
    reacquire: Callable[[AgentWalletRegistration], WriterLease], *, now_ns: int,
) -> WriterLease:
    _validate_rotation_args(
        lease, old_registration, new_registration, recorder, reacquire, now_ns)
    assessment = assess(old_registration, now_ns)
    identity = lease.authority.identity
    if old_registration.wallet_fingerprint == new_registration.wallet_fingerprint:
        _record_abort(
            recorder, identity, old_registration, new_registration, assessment,
            "same_wallet", now_ns)
        raise WriterLeaseError("wallet rotation uses the same wallet")
    if assessment == "active":
        _record_abort(
            recorder, identity, old_registration, new_registration, assessment,
            "not_due", now_ns)
        raise WriterLeaseError("wallet rotation is not due")
    try:
        lease.release()
    except Exception:
        _record_abort(
            recorder, identity, old_registration, new_registration, assessment,
            "release_failed", now_ns)
        raise
    try:
        new_lease = reacquire(new_registration)
    except Exception:
        _record_abort(
            recorder, identity, old_registration, new_registration, assessment,
            "acquire_failed", now_ns)
        raise
    new_authority = new_lease.authority
    if new_authority.mode == "cancel_only":
        _record_lease_abort(
            recorder, new_lease, identity, old_registration, new_registration,
            assessment, "acquire_failed", now_ns)
        new_lease.release()
        raise WriterLeaseError("rotation aborted: acquire_failed (contended)")
    expected = (identity.account_id, identity.instance_id, new_registration.wallet_fingerprint)
    observed_identity = new_authority.identity
    observed = (
        observed_identity.account_id, observed_identity.instance_id,
        observed_identity.wallet_fingerprint,
    )
    if observed != expected:
        _record_lease_abort(
            recorder, new_lease, identity, old_registration, new_registration,
            assessment, "identity_changed", now_ns)
        new_lease.release()
        raise WriterLeaseError("rotation aborted: identity_changed")
    decision = _rotation_decision(
        identity, old_registration, new_registration, assessment, "rotated",
        "rotation_completed", now_ns)
    try:
        recorder(decision)
    except BaseException as exc:
        raise RotationRecordError(new_lease, exc) from exc
    return new_lease


def assess(registration: AgentWalletRegistration, now_ns: int) -> WalletAssessment:
    if not isinstance(registration, AgentWalletRegistration):
        raise TypeError("registration must be an AgentWalletRegistration")
    if type(now_ns) is not int:
        raise TypeError("now_ns must be an integer")
    if now_ns < registration.issued_ns:
        raise ValueError("now_ns predates wallet issuance")
    if now_ns >= registration.expires_ns:
        return "expired"
    if now_ns >= registration.expires_ns - ROTATION_LEAD_NS:
        return "rotation_due"
    return "active"
