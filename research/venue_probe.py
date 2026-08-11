"""Pure validation for user-redacted Hyperliquid venue-probe evidence."""

import re
from collections.abc import Mapping

DAY_MS = 86_400_000
ROW_FIELDS = frozenset(
    {
        "probe_id",
        "attempt_ordinal",
        "signer_slot",
        "http_status",
        "venue_status",
        "venue_error_code",
        "start_offset_ms",
        "elapsed_ms",
        "run_digest",
        "harness_revision",
    }
)
PROBE_ORDINALS = {
    "B1_stale": frozenset({1}),
    "B1_duplicate": frozenset({1, 2}),
    "B2_revoked": frozenset({1, 2}),
    "B3_concurrent": frozenset({1}),
}
VENUE_STATUSES = frozenset({"ok", "err", "absent"})
SIGNER_SLOTS = frozenset({"A", "B"})
HEX_SHAPE = re.compile(r"0x[0-9a-fA-F]{38,}")
EXPECTED_PROBE_ROWS = frozenset(
    {
        ("B1_stale", 1, None),
        ("B1_duplicate", 1, None),
        ("B1_duplicate", 2, None),
        ("B2_revoked", 1, None),
        ("B2_revoked", 2, None),
        ("B3_concurrent", 1, "A"),
        ("B3_concurrent", 1, "B"),
    }
)


def _exact_int(value: object) -> bool:
    return type(value) is int


def _string_shape_errors(row: Mapping[object, object]) -> list[str]:
    errors = []
    for value in row.values():
        if not isinstance(value, str):
            continue
        if len(value) > 64:
            errors.append("string too long")
        if HEX_SHAPE.search(value):
            errors.append("sensitive string shape")
    return errors


def _identity_errors(row: Mapping[object, object]) -> list[str]:
    errors = []
    probe_id = row.get("probe_id")
    if probe_id not in PROBE_ORDINALS:
        errors.append("invalid probe_id")
    ordinal = row.get("attempt_ordinal")
    if probe_id in PROBE_ORDINALS and (
        not _exact_int(ordinal) or ordinal not in PROBE_ORDINALS[probe_id]
    ):
        errors.append("invalid attempt_ordinal")

    slot = row.get("signer_slot")
    valid_slot = slot in SIGNER_SLOTS if probe_id == "B3_concurrent" else slot is None
    if not valid_slot:
        errors.append("invalid signer_slot")
    return errors


def _venue_errors(row: Mapping[object, object]) -> list[str]:
    errors = []
    status = row.get("venue_status")
    if status not in VENUE_STATUSES:
        errors.append("invalid venue_status")
    code = row.get("venue_error_code")
    valid_code = code is None if status != "err" else code is None or bool(code)
    if not valid_code or code is not None and not isinstance(code, str):
        errors.append("invalid venue_error_code")

    http_status = row.get("http_status")
    valid_http = http_status is None or _exact_int(http_status) and 100 <= http_status <= 599
    if not valid_http:
        errors.append("invalid http_status")
    return errors


def _timing_errors(row: Mapping[object, object]) -> list[str]:
    errors = []
    for field in ("start_offset_ms", "elapsed_ms"):
        value = row.get(field)
        if not _exact_int(value) or not 0 <= value < DAY_MS:
            errors.append(f"invalid {field}")
    return errors


def _metadata_errors(row: Mapping[object, object]) -> list[str]:
    errors = []
    digest = row.get("run_digest")
    valid_digest = isinstance(digest, str) and 0 < len(digest) <= 16
    valid_digest &= bool(digest) and all(char in "0123456789abcdef" for char in digest)
    if not valid_digest:
        errors.append("invalid run_digest")
    revision = row.get("harness_revision")
    if not isinstance(revision, str) or not revision:
        errors.append("invalid harness_revision")
    return errors


def venue_probe_row_errors(row: object) -> tuple[str, ...]:
    """Return every structural or redaction error in one probe-result row."""
    if not isinstance(row, Mapping):
        return ("row must be a mapping",)
    errors = _string_shape_errors(row)
    if set(row) != ROW_FIELDS:
        errors.append("invalid row fields")
    errors.extend(_identity_errors(row))
    errors.extend(_venue_errors(row))
    errors.extend(_timing_errors(row))
    errors.extend(_metadata_errors(row))
    return tuple(errors)


def _probe_row_identity(row: object) -> tuple[object, object, object]:
    if not isinstance(row, Mapping):
        return (None, None, None)
    return row.get("probe_id"), row.get("attempt_ordinal"), row.get("signer_slot")


def validate_probe_dataset(rows: object) -> tuple[str, ...]:
    """Validate the complete seven-row, single-run probe evidence set."""
    if not isinstance(rows, list):
        return ("dataset must be a list",)
    errors = [
        f"row {index}: {error}"
        for index, row in enumerate(rows)
        for error in venue_probe_row_errors(row)
    ]
    identities = {_probe_row_identity(row) for row in rows}
    if len(rows) != len(EXPECTED_PROBE_ROWS) or identities != EXPECTED_PROBE_ROWS:
        errors.append("invalid probe row set")
    digests = {
        row.get("run_digest") for row in rows if isinstance(row, Mapping)
    }
    if len(digests) != 1:
        errors.append("mixed run_digest")
    return tuple(errors)
