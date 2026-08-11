import pytest

from execution import nonce
from execution.writer import WriterIdentity, WriterLease, WriterLeaseError

ACCOUNT_DIGEST = "b" * 64
NOW_MS = 5 * 86_400_000


def _release(resource) -> None:
    try:
        resource.release()
    except (nonce.SignerFenceError, WriterLeaseError):
        pass


@pytest.fixture
def bound_pair(tmp_path):
    fence = nonce.SignerFence.acquire(tmp_path, "a" * 64, "writer-one")
    identity = WriterIdentity(
        "hyperliquid:test-account", "writer-one", "a" * 64, "boot-one"
    )
    lease = WriterLease.acquire(tmp_path, identity, [].append, acquired_ns=100)
    yield fence, lease
    _release(lease)
    _release(fence)


def _initialize(target, fence, lease) -> None:
    target.__init__(
        fence,
        lease,
        account_digest=ACCOUNT_DIGEST,
        replayed_last=0,
        replayed_freeze_reason=None,
        recorder=lambda payload: None,
    )


def _construct(fence, lease):
    target = nonce.NonceAllocator.__new__(nonce.NonceAllocator)
    _initialize(target, fence, lease)
    return target


def _replace_identity(lease, **changes) -> None:
    identity = lease.authority.identity._replace(**changes)
    lease._authority = lease.authority._replace(identity=identity)


def _invalidate(lease) -> None:
    lease.path.unlink()
    lease.path.touch()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "wallet_fingerprint",
            "c" * 64,
            "signer fence wallet_fingerprint does not match writer lease identity",
        ),
        (
            "instance_id",
            "writer-two",
            "signer fence instance_id does not match writer lease identity",
        ),
    ],
)
def test_allocator_rejects_signer_writer_identity_mismatch(
    bound_pair, field: str, value: str, message: str,
) -> None:
    signer_fence, writer_lease = bound_pair
    _replace_identity(writer_lease, **{field: value})

    with pytest.raises(ValueError, match=message):
        _construct(signer_fence, writer_lease)


def test_matching_binding_derives_payload_instance_from_lease(bound_pair) -> None:
    signer_fence, writer_lease = bound_pair
    recorded = []
    allocator = nonce.NonceAllocator(
        signer_fence, writer_lease, account_digest=ACCOUNT_DIGEST,
        replayed_last=0, replayed_freeze_reason=None, recorder=recorded.append,
    )

    allocator.allocate(now_ms=NOW_MS, decided_ns=1)

    assert recorded[0]["instance_id"] == "writer-one"


def test_attribute_complete_lease_substitute_is_rejected(bound_pair) -> None:
    signer_fence, writer_lease = bound_pair
    class LeaseLike:
        authority = writer_lease.authority

        def revalidate(self):
            return self.authority

    with pytest.raises(TypeError, match="lease must be WriterLease"):
        _construct(signer_fence, LeaseLike())


def test_writer_lease_is_required(bound_pair) -> None:
    signer_fence, _ = bound_pair
    with pytest.raises(TypeError, match="lease"):
        nonce.NonceAllocator(
            signer_fence,
            account_digest=ACCOUNT_DIGEST,
            replayed_last=0,
            replayed_freeze_reason=None,
            recorder=lambda payload: None,
        )


def test_lease_revalidation_error_precedes_state_assignment(bound_pair) -> None:
    signer_fence, writer_lease = bound_pair
    target = nonce.NonceAllocator.__new__(nonce.NonceAllocator)
    _invalidate(writer_lease)

    with pytest.raises(WriterLeaseError, match="inode changed"):
        _initialize(target, signer_fence, writer_lease)
    assert vars(target) == {}


def test_lease_revalidation_precedes_identity_comparison(bound_pair) -> None:
    signer_fence, writer_lease = bound_pair
    _replace_identity(writer_lease, wallet_fingerprint="c" * 64)
    _invalidate(writer_lease)

    with pytest.raises(WriterLeaseError, match="inode changed"):
        _construct(signer_fence, writer_lease)


@pytest.mark.parametrize("mode", ["cancel_only", "pending_reconciliation"])
def test_allocator_construction_does_not_gate_writer_mode(
    bound_pair, mode: str,
) -> None:
    signer_fence, writer_lease = bound_pair
    writer_lease._authority = writer_lease.authority._replace(mode=mode)

    assert _construct(signer_fence, writer_lease)._instance_id == "writer-one"
