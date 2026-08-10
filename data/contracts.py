"""Pure, fail-closed contracts shared by collection and replay."""

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Literal

EVENT_KINDS = frozenset({"market", "decision", "order", "reconciliation", "ops"})
IDENTITY_STATUSES = frozenset({"known", "unknown"})
COMMON_FIELDS = (
    "schema_ver",
    "event_kind",
    "payload_schema",
    "venue",
    "conn_id",
    "boot_id",
    "recv_wall_ns",
    "recv_mono_ns",
    "source",
    "payload",
)


class ContractError(ValueError):
    """Raised when evidence cannot satisfy a frozen contract."""


@dataclass(frozen=True, slots=True)
class ArrivalIntervalAlert:
    """Descriptive market-arrival interval that cannot act as a hard gap verdict."""

    stream: str
    observed_ns: int
    soft_threshold_ns: int
    exceeded: bool
    severity: Literal["soft"] = "soft"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _valid_ns(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_envelope(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the common envelope plus order and quarantine invariants."""
    _require(isinstance(event, dict), "event must be a mapping")
    for field in COMMON_FIELDS:
        _require(field in event, f"missing common field: {field}")

    _require(type(event["schema_ver"]) is int and event["schema_ver"] > 0, "invalid schema_ver")
    _require(event["event_kind"] in EVENT_KINDS, "invalid event_kind")
    for field in ("payload_schema", "venue", "conn_id", "boot_id", "source"):
        _require(_nonempty_text(event[field]), f"invalid {field}")
    for field in ("recv_wall_ns", "recv_mono_ns"):
        _require(_valid_ns(event[field]), f"invalid {field}")
    _require(isinstance(event["payload"], dict), "payload must be a mapping")

    if event["event_kind"] == "order":
        _validate_order_identity(event)
    if event["event_kind"] == "ops" and event["payload_schema"] == "raw_quarantine":
        _require(_nonempty_text(event["payload"].get("raw")), "raw quarantine requires raw frame")
    return event


def _validate_order_identity(event: dict[str, Any]) -> None:
    _require(event.get("identity_status") in IDENTITY_STATUSES, "invalid identity_status")
    for field in ("client_order_id", "venue_order_id"):
        value = event.get(field)
        _require(value is None or _nonempty_text(value), f"invalid {field}")
    if event["payload_schema"] == "order_request":
        has_client_id = _nonempty_text(event.get("client_order_id"))
        _require(has_client_id, "order request needs client_order_id")


def bybit_update_gap(previous_u: int | None, current_u: int, message_type: str) -> bool:
    """Detect a discontinuous Bybit update ID; snapshots establish a new base."""
    _require(message_type in {"snapshot", "delta"}, "invalid Bybit message_type")
    _require(previous_u is None or _valid_ns(previous_u), "invalid previous_u")
    _require(_valid_ns(current_u), "invalid current_u")
    if message_type == "snapshot" or previous_u is None:
        return False
    return current_u != previous_u + 1


def hl_arrival_interval_alert(
    stream: str,
    previous_ns: int,
    current_ns: int,
    soft_threshold_ns: int,
) -> ArrivalIntervalAlert:
    """Describe a Hyperliquid arrival interval without issuing a hard gap verdict."""
    _require(_nonempty_text(stream), "invalid stream")
    _require(_valid_ns(previous_ns), "invalid previous_ns")
    _require(_valid_ns(current_ns), "invalid current_ns")
    _require(
        type(soft_threshold_ns) is int and soft_threshold_ns > 0,
        "invalid soft_threshold_ns",
    )
    _require(current_ns >= previous_ns, "arrival time moved backwards")
    observed_ns = current_ns - previous_ns
    return ArrivalIntervalAlert(
        stream=stream,
        observed_ns=observed_ns,
        soft_threshold_ns=soft_threshold_ns,
        exceeded=observed_ns > soft_threshold_ns,
    )


def monotonic_elapsed_ns(previous: dict[str, Any], current: dict[str, Any]) -> int:
    """Measure elapsed monotonic time only inside one host boot."""
    validate_envelope(previous)
    validate_envelope(current)
    _require(previous["boot_id"] == current["boot_id"], "boot_id changed")
    elapsed = current["recv_mono_ns"] - previous["recv_mono_ns"]
    _require(elapsed >= 0, "monotonic time moved backwards")
    return elapsed


def checksum_matches(payload: bytes, expected_sha256: str) -> bool:
    """Compare a byte payload with a lowercase SHA-256 digest."""
    _require(isinstance(payload, bytes), "checksum payload must be bytes")
    _require(_valid_digest(expected_sha256), "invalid sha256")
    return hmac.compare_digest(hashlib.sha256(payload).hexdigest(), expected_sha256)


def _valid_digest(value: Any) -> bool:
    is_hex = isinstance(value, str) and all(c in "0123456789abcdef" for c in value)
    return is_hex and len(value) == 64


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate the structure of an append-only raw-file manifest."""
    _require(isinstance(manifest, dict), "manifest must be a mapping")
    _require(type(manifest.get("schema_ver")) is int and manifest["schema_ver"] > 0, "schema_ver")
    files = manifest.get("files")
    _require(isinstance(files, list) and bool(files), "manifest files")
    for entry in files:
        _require(isinstance(entry, dict), "manifest file entry")
        _require(_nonempty_text(entry.get("path")), "invalid path")
        _require(_valid_digest(entry.get("sha256")), "invalid sha256")
        _require(_valid_ns(entry.get("bytes")), "invalid bytes")
    return manifest
