import pytest

from execution import nonce
from execution.writer import WriterIdentity, WriterLease, WriterLeaseError

FINGERPRINT = "a" * 64
ACCOUNT_DIGEST = "b" * 64
INSTANCE_ID = "writer-one"
DAY_MS = 86_400_000
NOW_MS = 5 * DAY_MS


def _event(
    reason: str = "clock_backward", fingerprint: str = FINGERPRINT, **changes,
) -> dict:
    event = {
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
    event["payload"].update(changes)
    return event


def _allocated(
    allocated: int, previous: int, *, fingerprint: str = FINGERPRINT,
    instance_id: str = INSTANCE_ID,
) -> dict:
    return _event(
        "nonce_allocated", fingerprint, allocated_nonce=allocated,
        previous_nonce=previous, outcome="allocated", instance_id=instance_id,
    )


def _release(resource) -> None:
    try:
        resource.release()
    except (nonce.SignerFenceError, WriterLeaseError):
        pass


@pytest.fixture
def make_nonce_allocator(tmp_path, request):
    def make(
        *, replayed_last=0, replayed_freeze_reason=None, recorder=...,
        fence=None, include_freeze_reason=True,
    ):
        selected_fence = fence or nonce.SignerFence.acquire(
            tmp_path, FINGERPRINT, INSTANCE_ID
        )
        if fence is None:
            request.addfinalizer(lambda: _release(selected_fence))
        identity = WriterIdentity(
            "hyperliquid:test-account", INSTANCE_ID, FINGERPRINT, "boot-one"
        )
        lease = WriterLease.acquire(tmp_path, identity, [].append, acquired_ns=100)
        request.addfinalizer(lambda: _release(lease))
        recorded = []
        values = {
            "account_digest": ACCOUNT_DIGEST, "replayed_last": replayed_last,
            "recorder": recorded.append if recorder is ... else recorder,
        }
        if include_freeze_reason:
            values["replayed_freeze_reason"] = replayed_freeze_reason
        allocator = nonce.NonceAllocator(selected_fence, lease, **values)
        return allocator, selected_fence, recorded

    return make


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


def test_allocator_requires_replayed_freeze_reason_keyword(
    make_nonce_allocator,
) -> None:
    with pytest.raises(TypeError, match="replayed_freeze_reason"):
        make_nonce_allocator(include_freeze_reason=False)


@pytest.mark.parametrize(
    ("value", "error", "message"),
    [(False, TypeError, "replayed_freeze_reason"),
     ("unknown", ValueError, "freeze reason")],
)
def test_allocator_rejects_invalid_replayed_freeze_reason(
    make_nonce_allocator, value, error, message,
) -> None:
    with pytest.raises(error, match=message):
        make_nonce_allocator(replayed_freeze_reason=value)


def test_replayed_freeze_is_readonly_and_absorbs_before_time_validation(
    make_nonce_allocator,
) -> None:
    allocator, fence, recorded = make_nonce_allocator(
        replayed_freeze_reason="clock_backward",
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


def test_clock_backward_freeze_records_advanced_previous_nonce_once(
    make_nonce_allocator,
) -> None:
    allocator, fence, recorded = make_nonce_allocator(
        replayed_last=NOW_MS + DAY_MS - 2,
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


def test_real_fence_invalidation_freezes_before_candidate_creation(
    make_nonce_allocator,
) -> None:
    allocator, fence, recorded = make_nonce_allocator(replayed_last=NOW_MS)
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


def test_freeze_recorder_failure_leaves_memory_absorbed(make_nonce_allocator) -> None:
    attempts = []

    def fail_frozen(payload) -> None:
        attempts.append(payload)
        if payload["outcome"] == "frozen":
            raise OSError("durable freeze write failed")

    allocator, fence, _ = make_nonce_allocator(
        replayed_last=NOW_MS + DAY_MS - 2, recorder=fail_frozen,
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


def test_non_fence_revalidation_error_propagates_without_freezing(
    tmp_path, make_nonce_allocator,
) -> None:
    class ExplodingFence(nonce.SignerFence):
        explode = False

        def revalidate(self) -> None:
            if self.explode:
                raise OSError("unexpected revalidation failure")
            super().revalidate()

    fence = ExplodingFence.acquire(tmp_path, FINGERPRINT, INSTANCE_ID)
    recorded = []
    allocator, _, _ = make_nonce_allocator(fence=fence, recorder=recorded.append)
    fence.explode = True
    with pytest.raises(OSError, match="unexpected revalidation failure"):
        allocator.allocate(now_ms=NOW_MS, decided_ns=1)
    assert allocator.frozen_reason is None
    assert allocator.last_nonce == 0 and recorded == []
    fence.explode = False
    fence.release()


@pytest.mark.parametrize("events", [
    [],
    [_event()],
    [_allocated(11, 10), _allocated(12, 11), _allocated(13, 12)],
    [_allocated(11, 10, instance_id="old"), _allocated(12, 11, instance_id="new")],
])
def test_conflict_replay_accepts_clean_or_truncated_chains(events) -> None:
    assert nonce.replay_signer_nonce_conflict(events, FINGERPRINT) is None


def test_conflict_replay_reports_the_first_chain_break() -> None:
    events = [_allocated(11, 10), _allocated(13, 12)]
    assert nonce.replay_signer_nonce_conflict(events, FINGERPRINT) == (
        "signer_nonce_conflict:chain_break:11:12"
    )


def test_conflict_replay_classifies_a_duplicate_nonce_as_a_chain_break() -> None:
    events = [_allocated(11, 10), _allocated(11, 10)]
    assert nonce.replay_signer_nonce_conflict(events, FINGERPRINT) == (
        "signer_nonce_conflict:chain_break:11:10"
    )


def test_conflict_replay_rejects_allocation_after_freeze() -> None:
    events = [_allocated(11, 10), _event(previous_nonce=11), _allocated(12, 11)]
    assert nonce.replay_signer_nonce_conflict(events, FINGERPRINT) == (
        "signer_nonce_conflict:allocation_after_freeze:clock_backward"
    )


def test_conflict_replay_ignores_interleaved_other_signer_rows() -> None:
    other = "c" * 64
    events = [_allocated(11, 10), _allocated(99, 0, fingerprint=other), _allocated(12, 11)]
    assert nonce.replay_signer_nonce_conflict(events, FINGERPRINT) is None


def test_conflict_replay_rejects_bool_nonce_fields() -> None:
    with pytest.raises(ValueError, match="allocated_nonce"):
        nonce.replay_signer_nonce_conflict([_allocated(True, 10)], FINGERPRINT)
    with pytest.raises(ValueError, match="previous_nonce"):
        nonce.replay_signer_nonce_conflict([_allocated(11, True)], FINGERPRINT)


def test_conflict_replay_validates_rows_after_a_conflict() -> None:
    events = [_allocated(11, 10), _allocated(13, 12), _allocated(True, 13)]
    with pytest.raises(ValueError, match="allocated_nonce"):
        nonce.replay_signer_nonce_conflict(events, FINGERPRINT)


def test_conflict_replay_rejects_unknown_or_mismatched_outcomes() -> None:
    with pytest.raises(ValueError, match="outcome"):
        nonce.replay_signer_nonce_conflict([_event(outcome="unknown")], FINGERPRINT)
    with pytest.raises(ValueError, match="reason"):
        nonce.replay_signer_nonce_conflict(
            [_event("clock_backward", outcome="allocated", allocated_nonce=11)],
            FINGERPRINT,
        )
    with pytest.raises(ValueError, match="reason"):
        nonce.replay_signer_nonce_conflict([_event("nonce_allocated")], FINGERPRINT)


def test_allocation_after_freeze_precedes_a_simultaneous_chain_break() -> None:
    events = [_allocated(11, 10), _event(previous_nonce=11), _allocated(13, 0)]
    assert nonce.replay_signer_nonce_conflict(events, FINGERPRINT) == (
        "signer_nonce_conflict:allocation_after_freeze:clock_backward"
    )


def test_multiple_freezes_preserve_the_first_reason_for_later_allocation() -> None:
    events = [
        _allocated(11, 10), _event("clock_backward", previous_nonce=11),
        _event("fence_invalidated", previous_nonce=11), _allocated(12, 11),
    ]
    assert nonce.replay_signer_nonce_conflict(events, FINGERPRINT) == (
        "signer_nonce_conflict:allocation_after_freeze:clock_backward"
    )
