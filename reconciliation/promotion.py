import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from execution.writer import WriterAuthority, WriterLease, WriterLeaseError
from reconciliation.admission import (
    CONTINUOUS_ADMISSION_REASON_KEYS,
    decide_continuous_admission,
)
from reconciliation.clock import StateClock
from reconciliation.exposure import ExposureClock
from reconciliation.fx import Notional
from reconciliation.legs import PairState
from reconciliation.state import AdmissionDecision, StartupContractError

DEMOTION_PREFIX = "writer_demoted:"
NONCE_FREEZE_KEY = "continuous_admission:nonce_frozen"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StartupContractError(message)


def _valid_positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


@dataclass(frozen=True, slots=True)
class WriterPromotionDecision:
    account_digest: str
    instance_id: str
    boot_id: str
    lease_epoch: int
    from_mode: str
    to_mode: str
    outcome: str
    reason: str
    admission_action: str
    admission_digest: str
    decided_ns: int

    def __post_init__(self) -> None:
        _require(_valid_digest(self.account_digest), "invalid promotion account digest")
        _require(_valid_digest(self.admission_digest), "invalid admission digest")
        _require(bool(self.instance_id) and isinstance(self.instance_id, str), "invalid instance")
        _require(bool(self.boot_id) and isinstance(self.boot_id, str), "invalid boot")
        _require(_valid_positive_int(self.lease_epoch), "invalid promotion lease epoch")
        _require(_valid_positive_int(self.decided_ns), "invalid promotion time")
        combination = (
            self.from_mode, self.to_mode, self.outcome, self.reason, self.admission_action
        )
        allowed = {
            ("pending_reconciliation", "risk_increasing", "promoted", "admission_ready", "ready"),
            (
                "pending_reconciliation", "pending_reconciliation", "denied",
                "admission_freeze", "cancel_only_freeze",
            ),
            ("cancel_only", "cancel_only", "denied", "not_promotable_mode", "ready"),
            ("cancel_only", "cancel_only", "denied", "not_promotable_mode", "cancel_only_freeze"),
        }
        _require(combination in allowed, "invalid promotion decision")


def _admission_digest(admission: AdmissionDecision) -> str:
    encoded = json.dumps(
        {"action": admission.action, "reasons": list(admission.reasons)},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _demotion_key(reason: str) -> str:
    key = NONCE_FREEZE_KEY if reason.startswith(f"{NONCE_FREEZE_KEY}:") else reason
    _require(key in CONTINUOUS_ADMISSION_REASON_KEYS, "unknown continuous admission reason")
    return key


def demotion_reason(admission: AdmissionDecision) -> str:
    """Encode bounded admission reason keys without copying dynamic payloads."""
    if not isinstance(admission, AdmissionDecision):
        raise TypeError("admission must be an AdmissionDecision")
    _require(admission.action == "cancel_only_freeze", "ready admission cannot demote")
    keys = tuple(_demotion_key(reason) for reason in admission.reasons)
    return DEMOTION_PREFIX + ",".join(keys)


def demote_writer(
    lease: WriterLease, admission: AdmissionDecision, *, now_ns: int
) -> WriterAuthority:
    """Revoke risk-increasing authority before recording the demotion."""
    if not isinstance(lease, WriterLease):
        raise TypeError("lease must be a WriterLease")
    if not isinstance(admission, AdmissionDecision):
        raise TypeError("admission must be an AdmissionDecision")
    if type(now_ns) is not int:
        raise TypeError("now_ns must be an integer")
    if now_ns <= 0:
        raise ValueError("now_ns must be positive")
    reason = demotion_reason(admission)
    return lease.demote_to_cancel_only(demotion_ns=now_ns, reason=reason)


def apply_continuous_admission(
    lease: WriterLease,
    *,
    exposure: ExposureClock | None,
    obligation: StateClock | None,
    pair: PairState,
    agent_wallet_status: str,
    nonce_freeze_reason: str | None,
    naked_notional: Notional | None,
    max_naked_notional: Notional,
    now_ns: int,
) -> AdmissionDecision:
    """Decide and enforce continuous admission as one indivisible operation."""
    if not isinstance(lease, WriterLease):
        raise TypeError("lease must be a WriterLease")
    if type(now_ns) is not int:
        raise TypeError("now_ns must be an integer")
    if now_ns <= 0:
        raise ValueError("now_ns must be positive")
    admission = decide_continuous_admission(
        exposure=exposure,
        obligation=obligation,
        pair=pair,
        agent_wallet_status=agent_wallet_status,
        nonce_freeze_reason=nonce_freeze_reason,
        naked_notional=naked_notional,
        max_naked_notional=max_naked_notional,
    )
    if admission.action != "ready":
        demote_writer(lease, admission, now_ns=now_ns)
    return admission


def _decision(
    authority: WriterAuthority, admission: AdmissionDecision, decided_ns: int,
    *, to_mode: str, outcome: str, reason: str,
) -> WriterPromotionDecision:
    identity = authority.identity
    return WriterPromotionDecision(
        hashlib.sha256(identity.account_id.encode()).hexdigest(),
        identity.instance_id, identity.boot_id, authority.lease_epoch,
        authority.mode, to_mode, outcome, reason, admission.action,
        _admission_digest(admission), decided_ns,
    )


def promote_writer(
    lease: WriterLease, admission: AdmissionDecision,
    recorder: Callable[[WriterPromotionDecision], None],
    *, now_ns: int,
) -> WriterAuthority:
    if not isinstance(lease, WriterLease):
        raise TypeError("lease must be a WriterLease")
    if not isinstance(admission, AdmissionDecision):
        raise TypeError("admission must be an AdmissionDecision")
    if not callable(recorder):
        raise TypeError("recorder must be callable")
    if type(now_ns) is not int:
        raise TypeError("now_ns must be an integer")
    if now_ns <= 0:
        raise ValueError("now_ns must be positive")
    authority = lease.authority
    if authority.mode == "risk_increasing":
        raise WriterLeaseError("writer already risk increasing")
    if authority.mode == "cancel_only":
        authority = lease.revalidate()
        recorder(_decision(
            authority, admission, now_ns, to_mode="cancel_only",
            outcome="denied", reason="not_promotable_mode",
        ))
        raise WriterLeaseError("writer authority not promotable")
    if admission.action == "ready":
        def record(current: WriterAuthority) -> None:
            recorder(_decision(
                current, admission, now_ns, to_mode="risk_increasing",
                outcome="promoted", reason="admission_ready",
            ))

        return lease.elevate_to_risk_increasing(
            promotion_ns=now_ns, before_elevate=record
        )
    authority = lease.revalidate()
    recorder(_decision(
        authority, admission, now_ns,
        to_mode="pending_reconciliation", outcome="denied", reason="admission_freeze",
    ))
    raise WriterLeaseError("admission freeze")
