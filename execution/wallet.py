from dataclasses import dataclass
from typing import Literal

DAY_NS = 86_400 * 1_000_000_000
VALIDITY_NS = 30 * DAY_NS
ROTATION_LEAD_NS = 7 * DAY_NS

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
        if self.expires_ns != self.issued_ns + VALIDITY_NS:
            raise ValueError("expires_ns must equal issued_ns plus validity")


def assess(registration: AgentWalletRegistration, now_ns: int) -> WalletAssessment:
    if type(now_ns) is not int:
        raise TypeError("now_ns must be an integer")
    if now_ns < registration.issued_ns:
        raise ValueError("now_ns predates wallet issuance")
    if now_ns >= registration.expires_ns:
        return "expired"
    if now_ns >= registration.expires_ns - ROTATION_LEAD_NS:
        return "rotation_due"
    return "active"
