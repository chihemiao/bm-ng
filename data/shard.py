"""Append-only hourly raw-record shards with deterministic replay."""

import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

from data.contracts import ContractError, checksum_matches, validate_manifest

MANIFEST_NAME = "manifest.jsonl"
SHARD_DIRECTORY = "shards"
LENGTH_BYTES = 8


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _hour_label(recv_wall_ns: int) -> str:
    _require(type(recv_wall_ns) is int and recv_wall_ns >= 0, "invalid recv_wall_ns")
    seconds = recv_wall_ns // 1_000_000_000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y%m%dT%H")


def _safe_boot_id(boot_id: str) -> bool:
    return bool(boot_id) and all(character.isalnum() or character in "-_." for character in boot_id)


class ShardWriter:
    """Write opaque records into non-overwriting UTC-hour gzip shards."""

    def __init__(self, root: Path, boot_id: str) -> None:
        _require(isinstance(root, Path), "root must be a Path")
        _require(_safe_boot_id(boot_id), "invalid boot_id")
        self.root = root
        self.boot_id = boot_id
        self.manifest_path = root / MANIFEST_NAME
        self.shard_root = root / SHARD_DIRECTORY
        self.shard_root.mkdir(parents=True, exist_ok=True)
        self._hour: str | None = None
        self._final_path: Path | None = None
        self._partial_path: Path | None = None
        self._raw_file: BinaryIO | None = None
        self._gzip_file: gzip.GzipFile | None = None
        self._records = 0
        self._first_ns: int | None = None
        self._last_ns: int | None = None
        self._closed = False

    def append(self, record: bytes, recv_wall_ns: int) -> None:
        _require(not self._closed, "writer is closed")
        _require(isinstance(record, bytes), "record must be bytes")
        hour = _hour_label(recv_wall_ns)
        if self._hour is not None:
            _require(hour >= self._hour, "UTC hour moved backwards")
        if hour != self._hour:
            self._finalize()
            self._open(hour)
        assert self._gzip_file is not None
        self._gzip_file.write(len(record).to_bytes(LENGTH_BYTES, "big"))
        self._gzip_file.write(record)
        self._records += 1
        self._first_ns = recv_wall_ns if self._first_ns is None else self._first_ns
        self._last_ns = recv_wall_ns

    def close(self) -> None:
        if not self._closed:
            self._finalize()
            self._closed = True

    def _open(self, hour: str) -> None:
        name = f"{hour}-{self.boot_id}.raw.gz"
        final_path = self.shard_root / name
        partial_path = final_path.with_suffix(final_path.suffix + ".partial")
        _require(not final_path.exists() and not partial_path.exists(), "shard path already exists")
        raw_file = partial_path.open("xb")
        self._hour = hour
        self._final_path = final_path
        self._partial_path = partial_path
        self._raw_file = raw_file
        self._gzip_file = gzip.GzipFile(filename="", mode="wb", fileobj=raw_file, mtime=0)
        self._records = 0
        self._first_ns = None
        self._last_ns = None

    def _finalize(self) -> None:
        if self._gzip_file is None:
            return
        self._gzip_file.close()
        assert self._raw_file is not None
        self._raw_file.flush()
        os.fsync(self._raw_file.fileno())
        self._raw_file.close()
        assert self._partial_path is not None and self._final_path is not None
        os.link(self._partial_path, self._final_path)
        self._partial_path.unlink()
        payload = self._final_path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        sidecar = self._final_path.with_suffix(self._final_path.suffix + ".sha256")
        with sidecar.open("x") as checksum_file:
            checksum_file.write(digest + "\n")
            checksum_file.flush()
            os.fsync(checksum_file.fileno())
        entry = {
            "path": self._final_path.relative_to(self.root).as_posix(),
            "sha256": digest,
            "bytes": len(payload),
            "boot_id": self.boot_id,
            "hour_utc": self._hour,
            "records": self._records,
            "first_recv_wall_ns": self._first_ns,
            "last_recv_wall_ns": self._last_ns,
        }
        line = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        with self.manifest_path.open("ab") as manifest:
            manifest.write(line)
            manifest.flush()
            os.fsync(manifest.fileno())
        self._gzip_file = None
        self._raw_file = None


def load_manifest(root: Path) -> dict:
    """Load append-only JSONL entries into the frozen manifest contract."""
    try:
        entries = [json.loads(line) for line in (root / MANIFEST_NAME).read_text().splitlines()]
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError("invalid manifest evidence") from error
    return validate_manifest({"schema_ver": 1, "files": entries})


def replay_records(root: Path) -> Iterator[bytes]:
    """Verify each finalized shard before yielding its opaque records."""
    root = root.resolve()
    for entry in load_manifest(root)["files"]:
        shard = (root / entry["path"]).resolve()
        _require(root in shard.parents, "manifest path escapes root")
        try:
            payload = shard.read_bytes()
            sidecar = shard.with_suffix(shard.suffix + ".sha256").read_text().strip()
        except OSError as error:
            raise ContractError("missing shard evidence") from error
        _require(sidecar == entry["sha256"], "sidecar checksum mismatch")
        _require(checksum_matches(payload, entry["sha256"]), "shard checksum mismatch")
        _require(len(payload) == entry["bytes"], "shard byte count mismatch")
        try:
            with gzip.open(shard, "rb") as compressed:
                while header := compressed.read(LENGTH_BYTES):
                    _require(len(header) == LENGTH_BYTES, "truncated record length")
                    size = int.from_bytes(header, "big")
                    record = compressed.read(size)
                    _require(len(record) == size, "truncated record")
                    yield record
        except OSError as error:
            raise ContractError("invalid gzip shard") from error
