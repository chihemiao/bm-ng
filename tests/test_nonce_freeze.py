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
