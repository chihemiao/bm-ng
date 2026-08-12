"""Pure validation for user-redacted Hyperliquid venue-probe evidence."""

import re
from collections.abc import Mapping
from typing import NamedTuple

from data.schema_nonce import DAY_MS as NONCE_DAY_MS
from data.schema_nonce import signer_nonce_window_bounds as signer_nonce_window_bounds

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


class ProbeNonces(NamedTuple):
    fresh: int
    stale: int


def _exact_int(value: object) -> bool:
    return type(value) is int


def probe_nonces(*, now_ms: int, stale_margin_ms: int) -> ProbeNonces:
    """Return one in-window nonce and one deliberately stale nonce."""
    for name, value in (("now_ms", now_ms), ("stale_margin_ms", stale_margin_ms)):
        if not _exact_int(value):
            raise TypeError(f"{name} must be an integer")
        if value <= 0:
            raise ValueError(f"{name} must be positive")
    stale = now_ms - 2 * NONCE_DAY_MS - stale_margin_ms
    if stale <= 0:
        raise ValueError("stale nonce must be positive")
    return ProbeNonces(fresh=now_ms + 1, stale=stale)


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


def normalize_venue_status(*, status_field: object) -> str:
    """Reduce a response to a closed status without inspecting response prose."""
    if type(status_field) is str and status_field in ("ok", "err"):
        return status_field
    return "absent"


def assemble_probe_row(
    *,
    probe_id,
    attempt_ordinal,
    signer_slot,
    http_status,
    venue_status,
    start_offset_ms,
    elapsed_ms,
    run_digest,
    harness_revision,
) -> Mapping[str, object]:
    """Build one validated row through a boundary with no raw-text input."""
    row = {
        "probe_id": probe_id,
        "attempt_ordinal": attempt_ordinal,
        "signer_slot": signer_slot,
        "http_status": http_status,
        "venue_status": venue_status,
        "venue_error_code": None,
        "start_offset_ms": start_offset_ms,
        "elapsed_ms": elapsed_ms,
        "run_digest": run_digest,
        "harness_revision": harness_revision,
    }
    errors = venue_probe_row_errors(row)
    if errors:
        raise ValueError("invalid probe row: " + "; ".join(errors))
    return row


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


def _validated_index(
    rows: object,
) -> dict[tuple[object, object, object], Mapping[object, object]]:
    errors = validate_probe_dataset(rows)
    if errors:
        raise ValueError("invalid probe dataset: " + "; ".join(errors))
    return {
        _probe_row_identity(row): row
        for row in rows
        if isinstance(row, Mapping)
    }


def _conclusive(row: Mapping[object, object]) -> bool:
    return row["http_status"] == 200 and row["venue_status"] in {"ok", "err"}


def _conclusive_ok(row: Mapping[object, object]) -> bool:
    return _conclusive(row) and row["venue_status"] == "ok"


def _single_probe_verdict(
    control: Mapping[object, object], experimental: Mapping[object, object]
) -> str:
    if not _conclusive_ok(control) or not _conclusive(experimental):
        return "inconclusive"
    return "confirms" if experimental["venue_status"] == "err" else "refutes"


def single_probe_verdicts(rows: object) -> dict[str, str]:
    """Classify the three probes with one control and one experimental result."""
    index = _validated_index(rows)
    duplicate_control = index[("B1_duplicate", 1, None)]
    return {
        "B1_stale": _single_probe_verdict(
            duplicate_control, index[("B1_stale", 1, None)]
        ),
        "B1_duplicate": _single_probe_verdict(
            duplicate_control, index[("B1_duplicate", 2, None)]
        ),
        "B2_revoked": _single_probe_verdict(
            index[("B2_revoked", 1, None)], index[("B2_revoked", 2, None)]
        ),
    }


def _strict_overlap(
    a_row: Mapping[object, object], b_row: Mapping[object, object]
) -> bool:
    a_start, a_elapsed = a_row["start_offset_ms"], a_row["elapsed_ms"]
    b_start, b_elapsed = b_row["start_offset_ms"], b_row["elapsed_ms"]
    return (
        a_elapsed > 0
        and b_elapsed > 0
        and a_start < b_start + b_elapsed
        and b_start < a_start + a_elapsed
    )


def _b3_verdict(
    a_row: Mapping[object, object], b_row: Mapping[object, object]
) -> str:
    if _conclusive_ok(a_row) and _conclusive_ok(b_row) and _strict_overlap(a_row, b_row):
        return "confirms"
    return "inconclusive"


def _error_code(row: Mapping[object, object]) -> object:
    if _conclusive(row) and row["venue_status"] == "err":
        return row["venue_error_code"]
    return None


def _b4_verdict(index: Mapping[tuple[object, object, object], Mapping]) -> str:
    if not _conclusive_ok(index[("B1_duplicate", 1, None)]) or not _conclusive_ok(
        index[("B2_revoked", 1, None)]
    ):
        return "inconclusive"
    nonce_codes = {
        code
        for key in (("B1_stale", 1, None), ("B1_duplicate", 2, None))
        if (code := _error_code(index[key])) is not None
    }
    auth_code = _error_code(index[("B2_revoked", 2, None)])
    if not nonce_codes or auth_code is None:
        return "inconclusive"
    return "confirms" if auth_code not in nonce_codes else "refutes"


def classify_probe_dataset(rows: object) -> dict[str, str]:
    """Validate independently, then return all five precommitted verdicts."""
    index = _validated_index(rows)
    verdicts = single_probe_verdicts(rows)
    verdicts.update(
        B3_concurrent=_b3_verdict(
            index[("B3_concurrent", 1, "A")], index[("B3_concurrent", 1, "B")]
        ),
        B4_error_class=_b4_verdict(index),
    )
    return verdicts
