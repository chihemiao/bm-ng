import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path

import pytest

from data.contracts import ContractError

shard_module = import_module("data.shard")
ShardWriter = shard_module.ShardWriter
load_manifest = shard_module.load_manifest
replay_records = shard_module.replay_records
ACCOUNT = "a" * 64
WALLET = "b" * 64
INSTANCE = "writer-one"


def _ns(hour: int, minute: int = 0, second: int = 0) -> int:
    instant = datetime(2026, 8, 10, hour, minute, second, tzinfo=timezone.utc)
    return int(instant.timestamp() * 1_000_000_000)


def _sidecar(root: Path, relative_path: str) -> Path:
    shard = root / relative_path
    return shard.with_suffix(shard.suffix + ".sha256")


def _ledger_event(sequence: int, *, entry_id: str | None = None) -> dict:
    wall = _ns(4, second=sequence)
    return {
        "schema_ver": 1,
        "event_kind": "reconciliation",
        "payload_schema": "account_ledger_entry",
        "venue": "hyperliquid",
        "conn_id": "reconciliation-1",
        "boot_id": "boot-a",
        "recv_wall_ns": wall,
        "recv_mono_ns": sequence,
        "source": "startup_reconciliation",
        "seq_within_boot": sequence,
        "payload": {
            "venue": "hyperliquid",
            "entry_id": entry_id or f"entry-{sequence}",
            "entry_kind": "funding",
            "occurred_ns": wall - 2,
            "asset": "USDC",
            "signed_amount_canonical": "1",
            "caused_by_order_id": None,
            "source_observed_ns": wall - 1,
        },
    }


def _writer_decision(sequence: int) -> dict:
    event = _ledger_event(sequence)
    event.update(
        event_kind="decision",
        payload_schema="writer_lease_decision",
        payload={
            "action": "acquire", "outcome": "pending_reconciliation",
            "reason": "lease_acquired", "account_digest": ACCOUNT,
            "instance_id": INSTANCE, "wallet_fingerprint": WALLET,
            "boot_id": "identity-boot", "lease_epoch": 1,
            "lock_path_digest": "c" * 64, "prior_epoch_valid": False,
        },
    )
    return event


def _nonce_decision(sequence: int, allocated_nonce: int | None = 7, **changes) -> dict:
    event = _ledger_event(sequence)
    allocated = allocated_nonce is not None
    payload = {
        "wallet_fingerprint": WALLET, "account_digest": ACCOUNT,
        "instance_id": INSTANCE, "allocated_nonce": allocated_nonce,
        "previous_nonce": allocated_nonce - 1 if allocated else 7,
        "now_ms": allocated_nonce - 1 if allocated else 7,
        "outcome": "allocated" if allocated else "frozen",
        "reason": "nonce_allocated" if allocated else "clock_backward",
        "decided_ns": sequence,
    }
    payload.update(changes)
    event.update(
        event_kind="decision", payload_schema="signer_nonce_allocation", payload=payload,
    )
    return event


def _order_request(
    sequence: int, *, client_order_id="0xrequest", venue="hyperliquid", **changes,
) -> dict:
    event = _writer_decision(sequence)
    payload = {
        "account_digest": ACCOUNT,
        "lease_epoch": 1,
        "writer_instance_id": INSTANCE,
        "wallet_fingerprint": WALLET,
        "allocated_nonce": None if venue == "bybit" else 7,
    }
    payload.update(changes)
    event.update(
        event_kind="order", payload_schema="order_request", payload=payload, venue=venue,
        identity_status="known", client_order_id=client_order_id, venue_order_id=None,
    )
    return event


def _replay_reasons(tmp_path: Path, *events: dict) -> tuple[str, ...]:
    writer = ShardWriter(tmp_path, boot_id="boot-a")
    for event in events:
        writer.append_event(event)
    writer.close()
    return shard_module.replay_event_window(tmp_path, _ns(4), _ns(5)).freeze_reasons


def test_hour_rotation_sidecars_append_only_manifest_and_replay(tmp_path: Path) -> None:
    records = [b'{"n":1}', b'{"n":2}']
    writer = ShardWriter(tmp_path, boot_id="boot-a")
    writer.append(records[0], recv_wall_ns=_ns(0, 59, 59))
    writer.append(records[1], recv_wall_ns=_ns(1))
    manifest_prefix = (tmp_path / "manifest.jsonl").read_bytes()
    writer.close()

    manifest_bytes = (tmp_path / "manifest.jsonl").read_bytes()
    assert manifest_bytes.startswith(manifest_prefix)
    manifest = load_manifest(tmp_path)
    assert manifest["schema_ver"] == 1
    assert len(manifest["files"]) == 2
    assert [entry["boot_id"] for entry in manifest["files"]] == ["boot-a", "boot-a"]
    assert all("boot-a" in Path(entry["path"]).name for entry in manifest["files"])

    for entry in manifest["files"]:
        payload = (tmp_path / entry["path"]).read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        assert entry["sha256"] == digest
        assert entry["bytes"] == len(payload)
        assert _sidecar(tmp_path, entry["path"]).read_text().strip() == digest

    assert list(replay_records(tmp_path)) == records


def test_boot_change_appends_without_overwriting_same_hour(tmp_path: Path) -> None:
    first = ShardWriter(tmp_path, boot_id="boot-a")
    first.append(b"first", recv_wall_ns=_ns(2))
    first.close()
    first_manifest = (tmp_path / "manifest.jsonl").read_bytes()
    first_entry = load_manifest(tmp_path)["files"][0]
    first_payload = (tmp_path / first_entry["path"]).read_bytes()

    second = ShardWriter(tmp_path, boot_id="boot-b")
    second.append(b"second", recv_wall_ns=_ns(2))
    second.close()

    manifest = load_manifest(tmp_path)
    assert (tmp_path / "manifest.jsonl").read_bytes().startswith(first_manifest)
    assert [entry["boot_id"] for entry in manifest["files"]] == ["boot-a", "boot-b"]
    assert len({entry["path"] for entry in manifest["files"]}) == 2
    assert (tmp_path / first_entry["path"]).read_bytes() == first_payload
    assert list(replay_records(tmp_path)) == [b"first", b"second"]


def test_replay_rejects_a_corrupted_shard(tmp_path: Path) -> None:
    writer = ShardWriter(tmp_path, boot_id="boot-a")
    writer.append(b"evidence", recv_wall_ns=_ns(3))
    writer.close()
    entry = load_manifest(tmp_path)["files"][0]
    shard = tmp_path / entry["path"]
    shard.write_bytes(shard.read_bytes() + b"corrupt")

    with pytest.raises(ContractError, match="checksum"):
        list(replay_records(tmp_path))


def test_canonical_event_append_and_exact_window_replay(tmp_path: Path) -> None:
    events = tuple(_ledger_event(sequence) for sequence in (1, 2, 3))
    legacy = deepcopy(events[0])
    legacy.update(event_kind="market", payload_schema="raw_frame", payload={"raw": "legacy"})
    legacy.pop("seq_within_boot")
    writer = ShardWriter(tmp_path, boot_id="boot-a")
    writer.append(shard_module.encode_event(legacy), legacy["recv_wall_ns"])
    for event in events:
        writer.append_event(event)
    writer.close()

    assert list(replay_records(tmp_path)) == [shard_module.encode_event(legacy)] + [
        shard_module.encode_event(event) for event in events
    ]
    replay = shard_module.replay_event_window(
        tmp_path, start_ns=events[1]["recv_wall_ns"], end_ns=events[2]["recv_wall_ns"]
    )
    assert replay.events == events[1:]
    assert replay.duplicate_digests == ()
    assert replay.freeze_reasons == ()
    assert replay.input_digest == shard_module.replay_event_window(
        tmp_path, start_ns=events[1]["recv_wall_ns"], end_ns=events[2]["recv_wall_ns"]
    ).input_digest


def test_writer_decision_uses_the_same_durable_window_replay(tmp_path: Path) -> None:
    event = _writer_decision(1)
    writer = ShardWriter(tmp_path, boot_id="boot-a")
    writer.append_event(event)
    writer.close()

    replay = shard_module.replay_event_window(tmp_path, _ns(4), _ns(5))
    assert replay.events == (event,)
    assert replay.freeze_reasons == ()


@pytest.mark.parametrize(
    ("field", "value"),
    (("instance_id", "writer-two"), ("lock_path_digest", "d" * 64)),
)
def test_replay_freezes_distinct_acquires_for_the_same_account_epoch(
    tmp_path: Path, field: str, value: str,
) -> None:
    first = _writer_decision(1)
    conflict = _writer_decision(2)
    conflict["payload"][field] = value
    writer = ShardWriter(tmp_path, boot_id="boot-a")
    for event in (first, first, conflict):
        writer.append(shard_module.encode_event(event), event["recv_wall_ns"])
    writer.close()

    replay = shard_module.replay_event_window(tmp_path, _ns(4), _ns(5))

    account = first["payload"]["account_digest"]
    assert replay.events == (first, conflict)
    assert len(replay.duplicate_digests) == 1
    assert replay.freeze_reasons == (
        f"writer_lease_decision:lease_epoch_conflict:{account}:1",
    )


def test_order_request_replay_checks_only_matching_window_leases(tmp_path: Path) -> None:
    acquire = _writer_decision(1)
    allocation = _nonce_decision(2)
    mismatch = _order_request(
        3, writer_instance_id="writer-two", wallet_fingerprint="c" * 64
    )
    outside = _order_request(4, lease_epoch=2, wallet_fingerprint="c" * 64)
    legacy = _order_request(5)
    legacy.pop("seq_within_boot")
    legacy["payload"] = {}
    writer = ShardWriter(tmp_path, boot_id="boot-a")
    for event in (acquire, allocation, mismatch, outside):
        writer.append_event(event)
    writer.append(shard_module.encode_event(legacy), legacy["recv_wall_ns"])
    writer.close()

    replay = shard_module.replay_event_window(tmp_path, _ns(4), _ns(5))

    assert replay.events == (acquire, allocation, mismatch, outside, legacy)
    assert replay.freeze_reasons == (
        "order_request:lease_binding_mismatch:" + "a" * 64 + ":1",
        "order_request:lease_wallet_mismatch:" + "a" * 64 + ":1",
    )
    rejected = ShardWriter(tmp_path / "legacy", boot_id="boot-a")
    with pytest.raises(ContractError, match="legacy order request"):
        rejected.append_event(legacy)
    rejected.close()


@pytest.mark.parametrize(
    "case", ["exact", "below_floor", "frozen_only", "same_client", "bybit"]
)
def test_order_nonce_join_accepts_safe_window_shapes(tmp_path: Path, case: str) -> None:
    allocation = _nonce_decision(1)
    request = _order_request(2)
    events = {
        "exact": (allocation, request),
        "below_floor": (allocation, _order_request(2, allocated_nonce=5)),
        "frozen_only": (_nonce_decision(1, None), _order_request(2, allocated_nonce=8)),
        "same_client": (allocation, request, _order_request(3)),
        "bybit": (allocation, _order_request(2, venue="bybit")),
    }[case]
    assert _replay_reasons(tmp_path, *events) == ()


def test_order_nonce_join_reports_missing_allocation_above_floor(tmp_path: Path) -> None:
    reasons = _replay_reasons(
        tmp_path, _nonce_decision(1), _order_request(2, allocated_nonce=8)
    )
    assert reasons == (f"order_request:nonce_allocation_missing:{WALLET}:8",)


def test_order_nonce_join_requires_allocation_to_precede_request(tmp_path: Path) -> None:
    reasons = _replay_reasons(
        tmp_path, _order_request(1, allocated_nonce=8), _nonce_decision(2, 8)
    )
    assert reasons == (f"order_request:nonce_allocation_not_prior:{WALLET}:8",)


@pytest.mark.parametrize(
    ("allocation_sequence", "request_sequence", "decided_ns", "recorded_ns", "expected"),
    [
        (1, 2, 999, 1, ()),
        (2, 1, 1, 999, (f"order_request:nonce_allocation_not_prior:{WALLET}:8",)),
    ],
    ids=("allocation-first", "allocation-last"),
)
def test_order_nonce_join_uses_event_order_not_payload_clocks(
    tmp_path: Path, allocation_sequence: int, request_sequence: int,
    decided_ns: int, recorded_ns: int, expected: tuple[str, ...],
) -> None:
    # These payload clocks have no schema relationship, so only durable event order is authority.
    allocation = _nonce_decision(allocation_sequence, 8, decided_ns=decided_ns)
    request = _order_request(request_sequence, allocated_nonce=8, recorded_ns=recorded_ns)
    events = sorted((allocation, request), key=lambda event: event["recv_wall_ns"])
    assert _replay_reasons(tmp_path, *events) == expected


@pytest.mark.parametrize(
    "changes", [{"account_digest": "d" * 64}, {"writer_instance_id": "writer-two"}]
)
def test_order_nonce_join_reports_signer_binding_mismatch(
    tmp_path: Path, changes: dict,
) -> None:
    reasons = _replay_reasons(tmp_path, _nonce_decision(1), _order_request(2, **changes))
    assert reasons == (f"order_request:nonce_allocation_binding_mismatch:{WALLET}:7",)


def test_other_wallet_allocation_does_not_satisfy_request_join(tmp_path: Path) -> None:
    reasons = _replay_reasons(
        tmp_path, _nonce_decision(1), _nonce_decision(2, 8, wallet_fingerprint="c" * 64),
        _order_request(3, allocated_nonce=8),
    )
    assert reasons == (f"order_request:nonce_allocation_missing:{WALLET}:8",)


def test_nonce_reuse_across_client_order_ids_freezes_once(tmp_path: Path) -> None:
    reasons = _replay_reasons(
        tmp_path, _nonce_decision(1), _order_request(2, client_order_id="0xone"),
        _order_request(3, client_order_id="0xtwo"),
    )
    assert reasons == (f"order_request:nonce_reuse:{WALLET}:7",)


def test_binding_mismatch_precedes_not_prior_reason(tmp_path: Path) -> None:
    reasons = _replay_reasons(
        tmp_path, _order_request(1), _nonce_decision(2, account_digest="d" * 64)
    )
    assert reasons == (f"order_request:nonce_allocation_binding_mismatch:{WALLET}:7",)


def test_replay_deduplicates_exact_events_and_freezes_conflicts(tmp_path: Path) -> None:
    first = _ledger_event(1, entry_id="same-entry")
    changed_entry = _ledger_event(2, entry_id="same-entry")
    changed_key = _ledger_event(2, entry_id="other-entry")
    writer = ShardWriter(tmp_path, boot_id="boot-a")
    for event in (first, first, changed_entry, changed_key):
        writer.append(shard_module.encode_event(event), event["recv_wall_ns"])
    writer.close()

    replay = shard_module.replay_event_window(tmp_path, start_ns=_ns(4), end_ns=_ns(5))

    assert replay.events == (first, changed_entry, changed_key)
    assert len(replay.duplicate_digests) == 1
    assert replay.freeze_reasons == (
        "account_ledger_entry:entry_id_conflict:same-entry",
        "replay:order_key_conflict",
    )


def test_event_replay_rejects_backward_order_and_unknown_schema(tmp_path: Path) -> None:
    writer = ShardWriter(tmp_path, boot_id="boot-a")
    for event in (_ledger_event(2), _ledger_event(1)):
        writer.append(shard_module.encode_event(event), event["recv_wall_ns"])
    writer.close()
    with pytest.raises(ContractError, match="order key"):
        shard_module.replay_event_window(tmp_path, start_ns=_ns(4), end_ns=_ns(5))

    unknown_root = tmp_path / "unknown"
    unknown = deepcopy(_ledger_event(1))
    unknown["payload_schema"] = "unknown_schema"
    writer = ShardWriter(unknown_root, boot_id="boot-a")
    writer.append(json.dumps(unknown, sort_keys=True, separators=(",", ":")).encode(), _ns(4, 1))
    writer.close()
    with pytest.raises(ContractError, match="payload_schema"):
        shard_module.replay_event_window(unknown_root, start_ns=_ns(4), end_ns=_ns(5))
