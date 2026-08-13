"""Pure, fail-closed contracts shared by collection and replay."""

import hashlib
import hmac
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from data import schema_order_request
from data.schema_dispatch import (
    COMMON_FIELDS,
    DURABLE_EVENT_SCHEMAS,
    EVENT_KINDS,
    IDENTITY_STATUSES,
    LEDGER_KINDS,
    PAYLOAD_SCHEMAS,
    PROMOTION_DECISIONS,
    RECONCILIATION_SCHEMAS,
    ROTATION_LEAD_NS,
    SURFACES,
    VALIDITY_NS,
    WRITER_DECISIONS,
)
from data.schema_nonce import signer_nonce_allocation_errors as _nonce_errors
from data.schema_wallet import wallet_rotation_semantic_errors as _wallet_errors


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
    validator = {
        "agent_wallet_rotation": _validate_wallet_rotation,
        "signer_nonce_allocation": _validate_signer_nonce,
        "writer_authority_promotion": _validate_writer_promotion,
        "writer_lease_decision": _validate_writer_decision,
    }.get(event["payload_schema"])
    if validator is not None:
        validator(event)
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
        "reconciliation_surface": _validate_surface, "account_ledger_entry": _validate_ledger_entry,
        "reconciliation_decision": _validate_decision,
    }
    validators[event["payload_schema"]](event)


def _validate_signer_nonce(event: dict[str, Any]) -> None:
    _require(event["event_kind"] == "decision", "invalid signer nonce event kind")
    _require(event["schema_ver"] == 1, "unsupported signer nonce schema version")
    errors = _nonce_errors(event["payload"])
    _require(not errors, errors[0] if errors else "")


def _validate_writer_decision(event: dict[str, Any]) -> None:
    _require(event["event_kind"] == "decision", "invalid writer decision event kind")
    _require(event["schema_ver"] == 1, "unsupported writer decision schema version")
    fields = set("account_digest action boot_id instance_id lease_epoch lock_path_digest outcome "
                 "prior_epoch_valid reason wallet_fingerprint".split())
    payload = _exact_fields(event["payload"], fields, "writer lease decision")
    for field in ("account_digest", "wallet_fingerprint", "lock_path_digest"):
        _require(_valid_digest(payload[field]), f"invalid {field}")
    for field in ("instance_id", "boot_id"):
        _require(_nonempty_text(payload[field]), f"invalid writer {field}")
    _require(type(payload["prior_epoch_valid"]) is bool, "invalid prior_epoch_valid")
    epoch = payload["lease_epoch"]
    _require(epoch is None or type(epoch) is int and epoch > 0, "invalid lease_epoch")
    combination, reason = (payload["action"], payload["outcome"]), payload["reason"]
    valid_reason = isinstance(reason, str) and reason in WRITER_DECISIONS.get(combination, ())
    if combination == ("demote", "cancel_only"):
        has_prefix = isinstance(reason, str) and reason.startswith("writer_demoted:")
        keys = reason.removeprefix("writer_demoted:").split(",") if has_prefix else []
        valid_reason = bool(keys and keys[0]) and keys == sorted(set(keys))
        key_prefix = "continuous_admission:"
        valid_reason &= all(key.startswith(key_prefix) and key.count(":") == 1 for key in keys)
    _require(valid_reason, "invalid writer decision combination")
    needs_epoch = payload["outcome"] != "terminated"
    _require(not needs_epoch or epoch is not None, "writer decision needs lease_epoch")


def _validate_writer_promotion(event: dict[str, Any]) -> None:
    _require(event["event_kind"] == "decision", "invalid writer promotion event kind")
    _require(event["schema_ver"] == 1, "unsupported writer promotion schema version")
    fields = set("account_digest admission_action admission_digest boot_id decided_ns from_mode "
                 "instance_id lease_epoch outcome reason to_mode".split())
    payload = _exact_fields(event["payload"], fields, "writer authority promotion")
    for field in ("account_digest", "admission_digest"):
        _require(_valid_digest(payload[field]), f"invalid {field}")
    for field in ("instance_id", "boot_id"):
        _require(_nonempty_text(payload[field]), f"invalid promotion {field}")
    valid_epoch = type(payload["lease_epoch"]) is int and payload["lease_epoch"] > 0
    _require(valid_epoch, "invalid lease_epoch")
    _require(type(payload["decided_ns"]) is int and payload["decided_ns"] > 0, "invalid decided_ns")
    key = tuple(payload[field] for field in ("outcome", "from_mode", "to_mode", "reason"))
    _require(payload["admission_action"] in PROMOTION_DECISIONS.get(key, ()),
             "invalid writer promotion combination")


def _validate_wallet_rotation(event: dict[str, Any]) -> None:
    _require(event["event_kind"] == "decision", "invalid wallet rotation event kind")
    _require(event["schema_ver"] == 1, "unsupported wallet rotation schema version")
    fields = set("account_digest assessment boot_id decided_ns instance_id new_expires_ns "
                 "new_issued_ns new_wallet_fingerprint old_expires_ns old_issued_ns "
                 "old_wallet_fingerprint outcome reason".split())
    payload = _exact_fields(event["payload"], fields, "agent wallet rotation")
    for field in ("account_digest", "old_wallet_fingerprint", "new_wallet_fingerprint"):
        _require(_valid_digest(payload[field]), f"invalid {field}")
    for field in ("instance_id", "boot_id"):
        _require(_nonempty_text(payload[field]), f"invalid {field}")
    times = ("old_issued_ns", "old_expires_ns", "new_issued_ns", "new_expires_ns", "decided_ns")
    for field in times:
        _require(type(payload[field]) is int and payload[field] > 0, f"invalid {field}")
    _require(payload["assessment"] in {"active", "rotation_due", "expired"}, "invalid assessment")
    _require(payload["outcome"] in {"rotated", "aborted"}, "invalid outcome")
    reasons = set("rotation_completed release_failed acquire_failed same_wallet "
                  "identity_changed not_due".split())
    _require(payload["reason"] in reasons, "invalid reason")
    errors = _wallet_errors(payload, validity_ns=VALIDITY_NS, rotation_lead_ns=ROTATION_LEAD_NS)
    _require(not errors, errors[0] if errors else "")


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
    legacy, errors = schema_order_request.order_request_event_binding(event)
    _require(not errors, errors[0] if errors else "")
    return legacy


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
