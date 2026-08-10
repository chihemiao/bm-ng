import hashlib
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
