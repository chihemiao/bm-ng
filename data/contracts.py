"""Pure, fail-closed contracts shared by collection and replay."""

import hashlib
import hmac
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

EVENT_KINDS = frozenset({"market", "decision", "order", "reconciliation", "ops"})
PAYLOAD_SCHEMAS = frozenset(
    {
        "bybit_sequence_gap",
        "account_ledger_entry",
        "collector_config",
        "liveness_failure",
        "order_observation",
        "order_request",
        "pre_ack_frame",
        "raw_frame",
        "raw_quarantine",
        "reconciliation_decision",
        "reconciliation_surface",
        "subscription_ack",
        "subscription_send",
        "venue_down",
        "venue_recovered",
        "writer_lease_decision",
    }
)
IDENTITY_STATUSES = frozenset({"known", "unknown"})
RECONCILIATION_SCHEMAS = frozenset(
    {"account_ledger_entry", "reconciliation_decision", "reconciliation_surface"}
)
DURABLE_EVENT_SCHEMAS = RECONCILIATION_SCHEMAS | {"order_request", "writer_lease_decision"}
ORDER_LEASE_FIELDS = ("account_digest", "lease_epoch", "writer_instance_id")
SURFACES = frozenset({"orders", "fills", "positions", "balances"})
LEDGER_KINDS = frozenset({"funding", "fee", "transfer", "adjustment"})
WRITER_DECISIONS = {
    ("acquire", "pending_reconciliation"): frozenset({"lease_acquired"}),
    ("deny", "cancel_only"): frozenset({"incumbent_other_wallet"}),
    ("deny", "terminated"): frozenset(
        {"shared_writer_identity", "unknown_incumbent", "unsafe_lock_file"}
    ),
    ("release", "released"): frozenset({"lease_released"}),
    ("revalidate", "invalidated"): frozenset({"lock_inode_changed"}),
}
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
    _require(event["payload_schema"] in PAYLOAD_SCHEMAS, "invalid payload_schema")
    for field in ("recv_wall_ns", "recv_mono_ns"):
        _require(_valid_ns(event[field]), f"invalid {field}")
    _require(isinstance(event["payload"], dict), "payload must be a mapping")

    if event["event_kind"] == "order":
        _validate_order_identity(event)
    if event["event_kind"] == "ops" and event["payload_schema"] == "raw_quarantine":
        _require(_nonempty_text(event["payload"].get("raw")), "raw quarantine requires raw frame")
    legacy_order = event["payload_schema"] == "order_request" and order_request_is_legacy(event)
    if event["payload_schema"] in DURABLE_EVENT_SCHEMAS and not legacy_order:
        _require(_valid_ns(event.get("seq_within_boot")), "invalid seq_within_boot")
    if event["payload_schema"] in RECONCILIATION_SCHEMAS:
        _validate_reconciliation(event)
    if event["payload_schema"] == "writer_lease_decision":
        _validate_writer_decision(event)
    return event


def _exact_fields(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == fields, f"invalid {label} fields")
    return value


def _window(value: Any, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    window = _exact_fields(value, {"start_ns", "end_ns"}, "window")
    _require(_valid_ns(window["start_ns"]) and _valid_ns(window["end_ns"]), "invalid window")
    _require(window["start_ns"] <= window["end_ns"], "window moved backwards")


def _canonical(value: Any) -> list[str]:
    fields = {"scheme_id", "scheme_version", "fingerprints"}
    canonical = _exact_fields(value, fields, "canonical set")
    _require(_nonempty_text(canonical["scheme_id"]), "invalid scheme_id")
    version = canonical["scheme_version"]
    _require(type(version) is int and version > 0, "invalid scheme_version")
    fingerprints = canonical["fingerprints"]
    valid = isinstance(fingerprints, list) and all(_nonempty_text(item) for item in fingerprints)
    _require(valid and len(fingerprints) == len(set(fingerprints)), "invalid fingerprints")
    return fingerprints


def _validate_reconciliation(event: dict[str, Any]) -> None:
    _require(event["event_kind"] == "reconciliation", "invalid reconciliation event kind")
    _require(event["schema_ver"] == 1, "unsupported reconciliation schema version")
    validators = {
        "reconciliation_surface": _validate_surface,
        "account_ledger_entry": _validate_ledger_entry,
        "reconciliation_decision": _validate_decision,
    }
    validators[event["payload_schema"]](event)


def _validate_writer_decision(event: dict[str, Any]) -> None:
    _require(event["event_kind"] == "decision", "invalid writer decision event kind")
    _require(event["schema_ver"] == 1, "unsupported writer decision schema version")
    fields = set(
        "account_digest action boot_id instance_id lease_epoch lock_path_digest outcome "
        "prior_epoch_valid reason wallet_fingerprint".split()
    )
    payload = _exact_fields(event["payload"], fields, "writer lease decision")
    for field in ("account_digest", "wallet_fingerprint", "lock_path_digest"):
        _require(_valid_digest(payload[field]), f"invalid {field}")
    for field in ("instance_id", "boot_id"):
        _require(_nonempty_text(payload[field]), f"invalid writer {field}")
    _require(type(payload["prior_epoch_valid"]) is bool, "invalid prior_epoch_valid")
    epoch = payload["lease_epoch"]
    _require(epoch is None or type(epoch) is int and epoch > 0, "invalid lease_epoch")
    reasons = WRITER_DECISIONS.get((payload["action"], payload["outcome"]), frozenset())
    _require(payload["reason"] in reasons, "invalid writer decision combination")
    needs_epoch = payload["outcome"] not in {"terminated"}
    _require(not needs_epoch or epoch is not None, "writer decision needs lease_epoch")


def _validate_surface(event: dict[str, Any]) -> None:
    fields = {
        "venue", "surface", "observed_ns", "fetched_count", "page_complete", "truncated",
        "unknown_count", "mismatch_count", "entities", "identities", "query_window",
    }
    payload = _exact_fields(event["payload"], fields, "reconciliation surface")
    _require(payload["venue"] == event["venue"], "surface venue differs from envelope")
    _require(payload["surface"] in SURFACES, "invalid surface")
    for field in ("observed_ns", "fetched_count", "unknown_count", "mismatch_count"):
        _require(_valid_ns(payload[field]), f"invalid {field}")
    _require(payload["observed_ns"] <= event["recv_wall_ns"], "surface observed in future")
    _require(type(payload["page_complete"]) is bool, "invalid page_complete")
    _require(type(payload["truncated"]) is bool, "invalid truncated")
    entities = _canonical(payload["entities"])
    identities = _canonical(payload["identities"])
    _require(len(entities) == len(identities), "surface cardinality differs")
    _require(payload["fetched_count"] == len(entities) + payload["unknown_count"], "fetched_count")
    _window(payload["query_window"], nullable=True)


def _validate_ledger_entry(event: dict[str, Any]) -> None:
    fields = {
        "venue", "entry_id", "entry_kind", "occurred_ns", "asset", "signed_amount_canonical",
        "caused_by_order_id", "source_observed_ns",
    }
    payload = _exact_fields(event["payload"], fields, "account ledger entry")
    _require(payload["venue"] == event["venue"], "ledger venue differs from envelope")
    _require(_nonempty_text(payload["entry_id"]), "invalid entry_id")
    _require(payload["entry_kind"] in LEDGER_KINDS, "invalid entry_kind")
    _require(_valid_ns(payload["occurred_ns"]), "invalid occurred_ns")
    _require(_valid_ns(payload["source_observed_ns"]), "invalid source_observed_ns")
    _require(payload["occurred_ns"] <= payload["source_observed_ns"], "ledger time moved backwards")
    _require(payload["source_observed_ns"] <= event["recv_wall_ns"], "ledger observed in future")
    _require(_nonempty_text(payload["asset"]), "invalid asset")
    amount = payload["signed_amount_canonical"]
    try:
        parsed = Decimal(amount) if isinstance(amount, str) else Decimal("NaN")
    except InvalidOperation:
        parsed = Decimal("NaN")
    canonical = "0" if parsed.is_finite() and parsed == 0 else format(parsed.normalize(), "f")
    _require(parsed.is_finite() and canonical == amount, "invalid canonical amount")
    order_id = payload["caused_by_order_id"]
    _require(order_id is None or _nonempty_text(order_id), "invalid caused_by_order_id")


def _validate_decision(event: dict[str, Any]) -> None:
    fields = set("action input_digest reasons schema_versions startup_started_ns window".split())
    payload = _exact_fields(event["payload"], fields, "reconciliation decision")
    _require(_valid_ns(payload["startup_started_ns"]), "invalid startup_started_ns")
    _require(payload["action"] in {"ready", "cancel_only_freeze"}, "invalid action")
    reasons = payload["reasons"]
    valid_reasons = isinstance(reasons, list) and all(_nonempty_text(item) for item in reasons)
    _require(valid_reasons and reasons == sorted(set(reasons)), "invalid reasons")
    _require(_valid_digest(payload["input_digest"]), "invalid input_digest")
    _window(payload["window"])
    versions = payload["schema_versions"]
    valid_versions = isinstance(versions, dict) and bool(versions)
    valid_versions &= all(_nonempty_text(key) for key in versions)
    valid_versions &= all(type(value) is int and value > 0 for value in versions.values())
    _require(valid_versions, "invalid schema_versions")


def _validate_order_identity(event: dict[str, Any]) -> None:
    _require(event.get("identity_status") in IDENTITY_STATUSES, "invalid identity_status")
    for field in ("client_order_id", "venue_order_id"):
        value = event.get(field)
        _require(value is None or _nonempty_text(value), f"invalid {field}")
    if event["payload_schema"] == "order_request":
        has_client_id = _nonempty_text(event.get("client_order_id"))
        _require(has_client_id, "order request needs client_order_id")


def order_request_is_legacy(event: dict[str, Any]) -> bool:
    payload = event["payload"]
    present = tuple(field in payload for field in ORDER_LEASE_FIELDS)
    has_sequence = "seq_within_boot" in event
    if not has_sequence and not any(present):
        return True
    _require(has_sequence and all(present), "invalid order request lease binding")
    account, epoch, instance = (payload[field] for field in ORDER_LEASE_FIELDS)
    valid = _valid_digest(account) and type(epoch) is int and epoch > 0
    valid &= _nonempty_text(instance)
    _require(valid, "invalid order request lease binding")
    return False


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
