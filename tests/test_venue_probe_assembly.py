import inspect

import pytest

import research.venue_probe as venue_probe

ASSEMBLY_PARAMETERS = (
    "probe_id",
    "attempt_ordinal",
    "signer_slot",
    "http_status",
    "venue_status",
    "start_offset_ms",
    "elapsed_ms",
    "run_digest",
    "harness_revision",
)


def _assemble(**changes):
    values = {
        "probe_id": "B1_stale",
        "attempt_ordinal": 1,
        "signer_slot": None,
        "http_status": 200,
        "venue_status": "err",
        "start_offset_ms": 1,
        "elapsed_ms": 2,
        "run_digest": "abcd1234",
        "harness_revision": "revision-1",
    }
    values.update(changes)
    return venue_probe.assemble_probe_row(**values)


def test_assembly_signature_is_a_closed_keyword_only_boundary():
    parameters = inspect.signature(venue_probe.assemble_probe_row).parameters
    assert tuple(parameters) == ASSEMBLY_PARAMETERS
    assert all(value.kind is inspect.Parameter.KEYWORD_ONLY for value in parameters.values())
    with pytest.raises(TypeError):
        _assemble(venue_error_code="response-text")


@pytest.mark.parametrize(
    ("probe_id", "ordinal", "slot"),
    [
        ("B1_stale", 1, None),
        ("B1_duplicate", 1, None),
        ("B1_duplicate", 2, None),
        ("B2_revoked", 1, None),
        ("B2_revoked", 2, None),
        ("B3_concurrent", 1, "A"),
        ("B3_concurrent", 1, "B"),
    ],
)
def test_assembly_emits_valid_rows_without_an_error_text_channel(
    probe_id, ordinal, slot
):
    row = _assemble(probe_id=probe_id, attempt_ordinal=ordinal, signer_slot=slot)
    assert row["venue_error_code"] is None
    assert venue_probe.venue_probe_row_errors(row) == ()


def test_assembly_rejects_any_row_contract_error():
    with pytest.raises(ValueError, match="invalid elapsed_ms"):
        _assemble(elapsed_ms=-1)
    assert (
        venue_probe.assemble_probe_row.__globals__["venue_probe_row_errors"]
        is venue_probe.venue_probe_row_errors
    )


def test_status_normalization_is_a_single_input_keyword_only_boundary():
    parameters = inspect.signature(venue_probe.normalize_venue_status).parameters
    assert tuple(parameters) == ("status_field",)
    assert parameters["status_field"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize(
    ("status_field", "expected"),
    [
        ("ok", "ok"),
        ("err", "err"),
        (None, "absent"),
        ("", "absent"),
        ("request rejected", "absent"),
        (123, "absent"),
        (True, "absent"),
        ({"status": "err"}, "absent"),
    ],
)
def test_status_normalization_uses_only_the_exact_status_field(status_field, expected):
    assert venue_probe.normalize_venue_status(status_field=status_field) == expected
