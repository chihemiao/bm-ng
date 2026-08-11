import hashlib
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from data.contracts import ContractError
from data.shard import ShardWriter
from execution import wallet as wallet_module
from execution.wallet import ROTATION_LEAD_NS, VALIDITY_NS, AgentWalletRegistration
from execution.writer import WriterIdentity, WriterLease, WriterLeaseError

ISSUED_NS = 1_000_000_000
DUE_NS = ISSUED_NS + VALIDITY_NS - ROTATION_LEAD_NS
HOLDER = """
import sys
from pathlib import Path
from execution.writer import WriterIdentity, WriterLease
lease = WriterLease.acquire(
    Path(sys.argv[1]), WriterIdentity(*sys.argv[2:]), [].append, acquired_ns=100
)
print(lease.authority.mode, flush=True)
sys.stdin.readline(); lease.release()
"""


class ProcessInterrupt(BaseException):
    pass


def _registration(fingerprint: str, issued_ns: int = ISSUED_NS):
    return AgentWalletRegistration(fingerprint, issued_ns, issued_ns + VALIDITY_NS)


def _identity(fingerprint: str, account="hyperliquid:test", instance="writer-one"):
    return WriterIdentity(account, instance, fingerprint, "boot-one")


def _lease(root: Path, fingerprint: str, account="hyperliquid:test", instance="writer-one"):
    identity = _identity(fingerprint, account, instance)
    return WriterLease.acquire(root, identity, [].append, acquired_ns=100)


def _under_contention(root: Path, incumbent: WriterIdentity, operation):
    fields = tuple(getattr(incumbent, name) for name in incumbent.__match_args__)
    process = subprocess.Popen(
        [sys.executable, "-B", "-u", "-c", HOLDER, str(root), *fields],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    ready, _, _ = select.select([process.stdout], [], [], 5)
    assert ready and process.stdout.readline().strip() == "pending_reconciliation"
    try:
        return operation()
    finally:
        process.communicate("\n", timeout=5)
        assert process.returncode == 0, process.stderr.read()


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


def test_true_release_failure_is_recorded_before_reraising(tmp_path: Path) -> None:
    old, new = _registration("a" * 64), _registration("b" * 64, ISSUED_NS + 1)
    lease, decisions = _lease(tmp_path, old.wallet_fingerprint), []
    assert lease._fd is not None
    os.close(lease._fd)
    with pytest.raises(OSError):
        wallet_module.rotate_agent_wallet(
            lease, old, new, decisions.append, lambda _: pytest.fail("reacquired"),
            now_ns=DUE_NS,
        )
    assert decisions == [_expected(
        old, new, DUE_NS, "rotation_due", "aborted", "release_failed")]


def test_real_reacquire_exception_records_acquire_failed(tmp_path: Path) -> None:
    old, new = _registration("a" * 64), _registration("b" * 64, ISSUED_NS + 1)
    lease, decisions = _lease(tmp_path, old.wallet_fingerprint), []

    def reacquire(_):
        incumbent = _identity("c" * 64)
        return _under_contention(
            tmp_path, incumbent, lambda: _lease(tmp_path, new.wallet_fingerprint))

    with pytest.raises(WriterLeaseError, match="shared writer identity"):
        wallet_module.rotate_agent_wallet(
            lease, old, new, decisions.append, reacquire, now_ns=DUE_NS)
    assert decisions == [_expected(
        old, new, DUE_NS, "rotation_due", "aborted", "acquire_failed")]


def test_cancel_only_reacquire_is_recorded_then_released(tmp_path: Path) -> None:
    old, new = _registration("a" * 64), _registration("b" * 64, ISSUED_NS + 1)
    lease, decisions, acquired = _lease(tmp_path, old.wallet_fingerprint), [], []
    def reacquire(_):
        incumbent = _identity("c" * 64, instance="competitor")
        acquired.append(_under_contention(
            tmp_path, incumbent, lambda: _lease(tmp_path, new.wallet_fingerprint)))
        return acquired[-1]

    def record(decision):
        assert acquired[-1].authority.mode == "cancel_only"
        decisions.append(decision)

    with pytest.raises(
        WriterLeaseError, match=r"^rotation aborted: acquire_failed \(contended\)$"
    ):
        wallet_module.rotate_agent_wallet(lease, old, new, record, reacquire, now_ns=DUE_NS)
    assert decisions == [_expected(
        old, new, DUE_NS, "rotation_due", "aborted", "acquire_failed")]
    with pytest.raises(WriterLeaseError, match="no writer authority"):
        _ = acquired[-1].authority


@pytest.mark.parametrize(
    ("account", "instance", "fingerprint"),
    [("hyperliquid:other", "writer-one", "b" * 64),
     ("hyperliquid:test", "writer-two", "b" * 64),
     ("hyperliquid:test", "writer-one", "c" * 64)],
)
def test_reacquired_identity_is_checked_before_recorded_cleanup(
    tmp_path: Path, account: str, instance: str, fingerprint: str
) -> None:
    old, new = _registration("a" * 64), _registration("b" * 64, ISSUED_NS + 1)
    lease, decisions, acquired = _lease(tmp_path, old.wallet_fingerprint), [], []

    def reacquire(_):
        acquired.append(_lease(tmp_path, fingerprint, account, instance))
        return acquired[-1]

    def record(decision):
        assert acquired[-1].authority.mode == "pending_reconciliation"
        decisions.append(decision)

    with pytest.raises(WriterLeaseError, match=r"^rotation aborted: identity_changed$"):
        wallet_module.rotate_agent_wallet(lease, old, new, record, reacquire, now_ns=DUE_NS)
    assert decisions == [_expected(
        old, new, DUE_NS, "rotation_due", "aborted", "identity_changed")]
    with pytest.raises(WriterLeaseError, match="no writer authority"):
        _ = acquired[-1].authority


@pytest.mark.parametrize("stage", ["release", "reacquire"])
def test_process_interrupt_is_not_recorded_as_rotation_failure(
    tmp_path: Path, stage: str
) -> None:
    old, new = _registration("a" * 64), _registration("b" * 64, ISSUED_NS + 1)
    decisions = []

    def writer_record(decision):
        if stage == "release" and decision.action == "release":
            raise ProcessInterrupt

    lease = WriterLease.acquire(
        tmp_path, _identity(old.wallet_fingerprint), writer_record, acquired_ns=100)

    def reacquire(_):
        if stage == "reacquire":
            raise ProcessInterrupt
        pytest.fail("reacquired after release interrupt")

    with pytest.raises(ProcessInterrupt):
        wallet_module.rotate_agent_wallet(
            lease, old, new, decisions.append, reacquire, now_ns=DUE_NS)
    assert decisions == []
