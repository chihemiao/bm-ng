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
