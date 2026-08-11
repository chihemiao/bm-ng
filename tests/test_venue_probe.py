import pytest

from research.venue_probe import DAY_MS, venue_probe_row_errors


def _row(**changes):
    row = {
        "probe_id": "B1_stale",
        "attempt_ordinal": 1,
        "signer_slot": None,
        "http_status": 200,
        "venue_status": "err",
        "venue_error_code": "nonce_stale",
        "start_offset_ms": 1,
        "elapsed_ms": 2,
        "run_digest": "abcd1234",
        "harness_revision": "revision-1",
    }
    row.update(changes)
    return row


def _assert_error(message, **changes):
    assert message in venue_probe_row_errors(_row(**changes))


@pytest.mark.parametrize(
    ("probe_id", "ordinal", "slot"),
    [
        ("B1_stale", 1, None),
        ("B1_duplicate", 2, None),
        ("B2_revoked", 2, None),
        ("B3_concurrent", 1, "A"),
        ("B3_concurrent", 1, "B"),
    ],
)
def test_valid_redacted_rows_pass(probe_id, ordinal, slot):
    assert venue_probe_row_errors(
        _row(probe_id=probe_id, attempt_ordinal=ordinal, signer_slot=slot)
    ) == ()


def test_row_fields_are_closed_and_complete():
    _assert_error("invalid row fields", extra="raw-response")
    row = _row()
    row.pop("http_status")
    assert "invalid row fields" in venue_probe_row_errors(row)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"probe_id": "B5_modify"}, "invalid probe_id"),
        ({"attempt_ordinal": 2}, "invalid attempt_ordinal"),
        ({"attempt_ordinal": True}, "invalid attempt_ordinal"),
        ({"signer_slot": "A"}, "invalid signer_slot"),
        ({"probe_id": "B3_concurrent", "signer_slot": None}, "invalid signer_slot"),
        ({"probe_id": "B3_concurrent", "signer_slot": "C"}, "invalid signer_slot"),
        ({"venue_status": "unknown"}, "invalid venue_status"),
        ({"venue_status": "ok"}, "invalid venue_error_code"),
        ({"venue_status": "absent"}, "invalid venue_error_code"),
        ({"http_status": True}, "invalid http_status"),
        ({"http_status": 99}, "invalid http_status"),
        ({"http_status": 600}, "invalid http_status"),
        ({"run_digest": "ABC"}, "invalid run_digest"),
        ({"run_digest": "a" * 17}, "invalid run_digest"),
        ({"harness_revision": ""}, "invalid harness_revision"),
    ],
)
def test_invalid_closed_values_are_rejected(changes, message):
    _assert_error(message, **changes)


@pytest.mark.parametrize("field", ["start_offset_ms", "elapsed_ms"])
@pytest.mark.parametrize("value", [-1, True, DAY_MS])
def test_relative_time_fields_reject_invalid_or_epoch_shaped_values(field, value):
    _assert_error(f"invalid {field}", **{field: value})


def test_all_string_values_reject_identifying_or_oversized_shapes():
    _assert_error("sensitive string shape", venue_error_code="0x" + "a" * 38)
    _assert_error("string too long", venue_error_code="x" * 65)
