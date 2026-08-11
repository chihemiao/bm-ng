import pytest

from execution import nonce

FINGERPRINT = "a" * 64
ACCOUNT_DIGEST = "b" * 64
INSTANCE_ID = "writer-one"
DAY_MS = 86_400_000
NOW_MS = 5 * DAY_MS


def _event(reason: str = "clock_backward", fingerprint: str = FINGERPRINT) -> dict:
    return {
        "schema_ver": 1, "event_kind": "decision",
        "payload_schema": "signer_nonce_allocation", "venue": "hyperliquid",
        "conn_id": INSTANCE_ID, "boot_id": "boot-one", "recv_wall_ns": 1,
        "recv_mono_ns": 1, "source": "nonce_allocator", "seq_within_boot": 1,
        "payload": {
            "wallet_fingerprint": fingerprint, "account_digest": ACCOUNT_DIGEST,
            "instance_id": INSTANCE_ID, "allocated_nonce": None,
            "previous_nonce": NOW_MS, "now_ms": NOW_MS, "outcome": "frozen",
            "reason": reason, "decided_ns": 1,
        },
    }


def _allocator(
    tmp_path, *, replayed_last: int = 0, replayed_freeze_reason=None, recorder=None,
):
    fence = nonce.SignerFence.acquire(tmp_path, FINGERPRINT, INSTANCE_ID)
    recorded = []
    allocator = nonce.NonceAllocator(
        fence, account_digest=ACCOUNT_DIGEST, instance_id=INSTANCE_ID,
        replayed_last=replayed_last, replayed_freeze_reason=replayed_freeze_reason,
        recorder=recorded.append if recorder is None else recorder,
    )
    return allocator, fence, recorded


def test_replay_freeze_reason_filters_signer_and_returns_one_reason() -> None:
    other = _event(fingerprint="c" * 64)
    allocated = _event(reason="nonce_allocated")
    allocated["payload"]["outcome"] = "allocated"
    assert nonce.replay_freeze_reason([], FINGERPRINT) is None
    assert nonce.replay_freeze_reason([other], FINGERPRINT) is None
    assert nonce.replay_freeze_reason([allocated], FINGERPRINT) is None
    assert nonce.replay_freeze_reason([other, allocated, _event()], FINGERPRINT) == "clock_backward"


@pytest.mark.parametrize("reasons", [
    ("clock_backward", "clock_backward"),
    ("clock_backward", "fence_invalidated"),
])
def test_replay_rejects_multiple_matching_freeze_rows(reasons: tuple[str, str]) -> None:
    with pytest.raises(ValueError, match="multiple"):
        nonce.replay_freeze_reason([_event(reason) for reason in reasons], FINGERPRINT)


def test_replay_rejects_unknown_freeze_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        nonce.replay_freeze_reason([_event("unknown")], FINGERPRINT)


def test_allocator_requires_replayed_freeze_reason_keyword(tmp_path) -> None:
    fence = nonce.SignerFence.acquire(tmp_path, FINGERPRINT, INSTANCE_ID)
    try:
        with pytest.raises(TypeError, match="replayed_freeze_reason"):
            nonce.NonceAllocator(
                fence, account_digest=ACCOUNT_DIGEST, instance_id=INSTANCE_ID,
                replayed_last=0, recorder=lambda payload: None,
            )
    finally:
        fence.release()


@pytest.mark.parametrize(
    ("value", "error", "message"),
    [(False, TypeError, "replayed_freeze_reason"),
     ("unknown", ValueError, "freeze reason")],
)
def test_allocator_rejects_invalid_replayed_freeze_reason(
    tmp_path, value, error, message,
) -> None:
    fence = nonce.SignerFence.acquire(tmp_path, FINGERPRINT, INSTANCE_ID)
    try:
        with pytest.raises(error, match=message):
            nonce.NonceAllocator(
                fence, account_digest=ACCOUNT_DIGEST, instance_id=INSTANCE_ID,
                replayed_last=0, replayed_freeze_reason=value,
                recorder=lambda payload: None,
            )
    finally:
        fence.release()


def test_replayed_freeze_is_readonly_and_absorbs_before_time_validation(tmp_path) -> None:
    allocator, fence, recorded = _allocator(
        tmp_path, replayed_freeze_reason="clock_backward",
    )
    assert issubclass(nonce.NonceFrozenError, RuntimeError)
    assert not hasattr(nonce, "NonceAllocationError")
    assert allocator.frozen_reason == "clock_backward"
    with pytest.raises(AttributeError):
        allocator.frozen_reason = None
    for now_ms, decided_ns in [(0, 0), (NOW_MS, 1)]:
        with pytest.raises(nonce.NonceFrozenError) as raised:
            allocator.allocate(now_ms=now_ms, decided_ns=decided_ns)
        assert raised.value.reason == "clock_backward"
    assert recorded == [] and allocator.last_nonce == 0
    fence.release()


def test_clock_backward_freeze_records_advanced_previous_nonce_once(tmp_path) -> None:
    allocator, fence, recorded = _allocator(
        tmp_path, replayed_last=NOW_MS + DAY_MS - 2,
    )
    allocated = allocator.allocate(now_ms=NOW_MS, decided_ns=1)
    assert allocated == NOW_MS + DAY_MS - 1
    with pytest.raises(nonce.NonceFrozenError) as raised:
        allocator.allocate(now_ms=NOW_MS, decided_ns=2)
    assert raised.value.reason == allocator.frozen_reason == "clock_backward"
    assert recorded[1] == {
        "wallet_fingerprint": FINGERPRINT, "account_digest": ACCOUNT_DIGEST,
        "instance_id": INSTANCE_ID, "allocated_nonce": None,
        "previous_nonce": allocated, "now_ms": NOW_MS, "outcome": "frozen",
        "reason": "clock_backward", "decided_ns": 2,
    }
    assert len(recorded) == 2 and allocator.last_nonce == allocated
    with pytest.raises(nonce.NonceFrozenError):
        allocator.allocate(now_ms=NOW_MS, decided_ns=3)
    assert len(recorded) == 2
    fence.release()


def test_real_fence_invalidation_freezes_before_candidate_creation(tmp_path) -> None:
    allocator, fence, recorded = _allocator(tmp_path, replayed_last=NOW_MS)
    fence.path.unlink()
    fence.path.touch()
    with pytest.raises(nonce.NonceFrozenError) as raised:
        allocator.allocate(now_ms=NOW_MS, decided_ns=9)
    assert raised.value.reason == allocator.frozen_reason == "fence_invalidated"
    assert recorded == [{
        "wallet_fingerprint": FINGERPRINT, "account_digest": ACCOUNT_DIGEST,
        "instance_id": INSTANCE_ID, "allocated_nonce": None,
        "previous_nonce": NOW_MS, "now_ms": NOW_MS, "outcome": "frozen",
        "reason": "fence_invalidated", "decided_ns": 9,
    }]
    assert allocator.last_nonce == NOW_MS
    with pytest.raises(nonce.NonceFrozenError):
        allocator.allocate(now_ms=0, decided_ns=0)
    assert len(recorded) == 1


def test_freeze_recorder_failure_leaves_memory_absorbed(tmp_path) -> None:
    attempts = []

    def fail_frozen(payload) -> None:
        attempts.append(payload)
        if payload["outcome"] == "frozen":
            raise OSError("durable freeze write failed")

    allocator, fence, _ = _allocator(
        tmp_path, replayed_last=NOW_MS + DAY_MS - 2, recorder=fail_frozen,
    )
    allocated = allocator.allocate(now_ms=NOW_MS, decided_ns=1)
    with pytest.raises(OSError, match="durable freeze write failed"):
        allocator.allocate(now_ms=NOW_MS, decided_ns=2)
    assert allocator.frozen_reason == "clock_backward"
    assert allocator.last_nonce == allocated
    assert len(attempts) == 2 and attempts[-1]["outcome"] == "frozen"
    with pytest.raises(nonce.NonceFrozenError) as raised:
        allocator.allocate(now_ms=0, decided_ns=0)
    assert raised.value.reason == "clock_backward" and len(attempts) == 2
    fence.release()


def test_non_fence_revalidation_error_propagates_without_freezing(tmp_path) -> None:
    class ExplodingFence(nonce.SignerFence):
        explode = False

        def revalidate(self) -> None:
            if self.explode:
                raise OSError("unexpected revalidation failure")
            super().revalidate()

    fence = ExplodingFence.acquire(tmp_path, FINGERPRINT, INSTANCE_ID)
    recorded = []
    allocator = nonce.NonceAllocator(
        fence, account_digest=ACCOUNT_DIGEST, instance_id=INSTANCE_ID,
        replayed_last=0, replayed_freeze_reason=None, recorder=recorded.append,
    )
    fence.explode = True
    with pytest.raises(OSError, match="unexpected revalidation failure"):
        allocator.allocate(now_ms=NOW_MS, decided_ns=1)
    assert allocator.frozen_reason is None
    assert allocator.last_nonce == 0 and recorded == []
    fence.explode = False
    fence.release()
