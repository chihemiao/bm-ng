import hashlib
from pathlib import Path

import pytest

from data.contracts import ContractError
from data.shard import ShardWriter
from execution import wallet as wallet_module
from execution.wallet import ROTATION_LEAD_NS, VALIDITY_NS, AgentWalletRegistration
from execution.writer import WriterIdentity, WriterLease, WriterLeaseError

ISSUED_NS = 1_000_000_000
DUE_NS = ISSUED_NS + VALIDITY_NS - ROTATION_LEAD_NS


def _registration(fingerprint: str, issued_ns: int = ISSUED_NS):
    return AgentWalletRegistration(fingerprint, issued_ns, issued_ns + VALIDITY_NS)


def _identity(fingerprint: str):
    return WriterIdentity("hyperliquid:test", "writer-one", fingerprint, "boot-one")


def _lease(root: Path, fingerprint: str) -> WriterLease:
    return WriterLease.acquire(root, _identity(fingerprint), [].append, acquired_ns=100)


def _expected(old, new, now_ns, assessment, outcome, reason):
    return wallet_module.AgentWalletRotationDecision(
        hashlib.sha256(b"hyperliquid:test").hexdigest(), "writer-one", "boot-one",
        old.wallet_fingerprint, new.wallet_fingerprint, old.issued_ns, old.expires_ns,
        new.issued_ns, new.expires_ns, assessment, outcome, reason, now_ns,
    )


def test_rotation_validates_structure_and_clock_before_side_effects(tmp_path: Path) -> None:
    old, new = _registration("a" * 64), _registration("b" * 64, ISSUED_NS + 1)
    lease, decisions = _lease(tmp_path, old.wallet_fingerprint), []
    valid = [lease, old, new, decisions.append, lambda _: None]
    for index, value in ((0, None), (1, None), (2, None), (3, None), (4, None)):
        arguments = valid.copy()
        arguments[index] = value
        with pytest.raises(TypeError):
            wallet_module.rotate_agent_wallet(*arguments, now_ns=DUE_NS)
    for now_ns, error in ((True, TypeError), (0, ValueError), (ISSUED_NS - 1, ValueError)):
        with pytest.raises(error):
            wallet_module.rotate_agent_wallet(*valid, now_ns=now_ns)
    assert decisions == [] and lease.revalidate() == lease.authority
    lease.release()


@pytest.mark.parametrize(
    ("same_wallet", "now_ns", "assessment", "reason"),
    [(True, DUE_NS, "rotation_due", "same_wallet"),
     (False, DUE_NS - 1, "active", "not_due")],
)
def test_rotation_records_preflight_abort_without_releasing(
    tmp_path: Path, same_wallet: bool, now_ns: int, assessment: str, reason: str
) -> None:
    old = _registration("a" * 64)
    new = _registration(old.wallet_fingerprint if same_wallet else "b" * 64, ISSUED_NS + 1)
    lease, decisions = _lease(tmp_path, old.wallet_fingerprint), []
    with pytest.raises(WriterLeaseError, match=reason.replace("_", " ")):
        wallet_module.rotate_agent_wallet(
            lease, old, new, decisions.append, lambda _: pytest.fail("reacquired"),
            now_ns=now_ns,
        )
    assert decisions == [_expected(old, new, now_ns, assessment, "aborted", reason)]
    closed = ShardWriter(tmp_path / "closed", "boot")
    closed.close()
    with pytest.raises(ContractError, match="closed"):
        wallet_module.rotate_agent_wallet(
            lease, old, new, lambda _: closed.append(b"decision", now_ns),
            lambda _: pytest.fail("reacquired"), now_ns=now_ns,
        )
    assert lease.revalidate() == lease.authority
    lease.release()


@pytest.mark.parametrize(("now_ns", "assessment"), [(DUE_NS, "rotation_due"),
                                                     (ISSUED_NS + VALIDITY_NS, "expired")])
def test_rotation_releases_reacquires_records_and_returns(
    tmp_path: Path, now_ns: int, assessment: str) -> None:
    old, new = _registration("a" * 64), _registration("b" * 64, ISSUED_NS + 1)
    lease, decisions = _lease(tmp_path, old.wallet_fingerprint), []
    acquired = []

    def reacquire(registration):
        assert registration is new
        acquired.append(_lease(tmp_path, registration.wallet_fingerprint))
        return acquired[-1]

    result = wallet_module.rotate_agent_wallet(
        lease, old, new, decisions.append, reacquire, now_ns=now_ns)
    assert result is acquired[0]
    assert decisions == [_expected(old, new, now_ns, assessment, "rotated", "rotation_completed")]
    with pytest.raises(WriterLeaseError, match="no writer authority"):
        _ = lease.authority
    result.release()


def test_success_record_failure_exposes_the_new_lease_for_cleanup(tmp_path: Path) -> None:
    old, new = _registration("a" * 64), _registration("b" * 64, ISSUED_NS + 1)
    lease, acquired = _lease(tmp_path, old.wallet_fingerprint), []
    closed = ShardWriter(tmp_path / "closed", "boot")
    closed.close()

    def reacquire(registration):
        acquired.append(_lease(tmp_path, registration.wallet_fingerprint))
        return acquired[-1]

    with pytest.raises(wallet_module.RotationRecordError) as caught:
        wallet_module.rotate_agent_wallet(
            lease, old, new, lambda _: closed.append(b"decision", DUE_NS),
            reacquire, now_ns=DUE_NS,
        )
    assert caught.value.lease is acquired[0]
    assert isinstance(caught.value.__cause__, ContractError)
    caught.value.lease.release()
