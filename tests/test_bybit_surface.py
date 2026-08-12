import importlib
import inspect

import pytest

from reconciliation.state import surface_is_authoritative


def _module():
    return importlib.import_module("reconciliation.bybit_surface")


def _payload(*rows, cursor=""):
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"category": "linear", "nextPageCursor": cursor, "list": list(rows)},
        "retExtInfo": {},
        "time": 1,
    }


def _parse(payload=None, *, symbol="BTC", observed_ns=100):
    value = _payload({"unparsed": "row"}) if payload is None else payload
    return _module().parse_bybit_positions_surface(
        value, symbol=symbol, observed_ns=observed_ns
    )


def test_valid_envelope_preserves_all_rows_as_unknown_until_row_contract_lands():
    evidence = _parse(_payload({"one": 1}, "opaque"), observed_ns=321)

    assert evidence.observed_ns == 321
    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (2, 2, 0)
    assert evidence.page_complete is True and evidence.truncated is False
    assert evidence.entities.scheme_id == "bybit.positions.state"
    assert evidence.identities.scheme_id == "bybit.positions.identity"
    assert evidence.entities.scheme_version == evidence.identities.scheme_version == 1
    assert evidence.entities.fingerprints == evidence.identities.fingerprints == frozenset()


def test_empty_per_symbol_result_is_incomplete_without_inventing_an_unknown_row():
    evidence = _parse(_payload())

    assert (evidence.fetched_count, evidence.unknown_count) == (0, 0)
    assert evidence.page_complete is False and evidence.truncated is False
    assert not surface_is_authoritative(evidence, now_ns=100, max_age_ns=1)


def test_nonempty_cursor_is_explicitly_truncated_and_incomplete():
    evidence = _parse(_payload({"row": 1}, cursor="next-page"))

    assert evidence.page_complete is False and evidence.truncated is True


@pytest.mark.parametrize("field", ["retCode", "retMsg", "result", "retExtInfo", "time"])
def test_top_level_common_response_fields_are_exact(field):
    missing = _payload()
    missing.pop(field)
    extra = _payload()
    extra["newField"] = 1

    with pytest.raises(ValueError, match="response fields"):
        _parse(missing)
    with pytest.raises(ValueError, match="response fields"):
        _parse(extra)


@pytest.mark.parametrize("field", ["category", "nextPageCursor", "list"])
def test_consumed_result_fields_are_exact(field):
    payload = _payload()
    payload["result"].pop(field)

    with pytest.raises(ValueError, match="result fields"):
        _parse(payload)


@pytest.mark.parametrize(
    "path,value,error",
    [
        (("retCode",), True, TypeError),
        (("retCode",), 1, ValueError),
        (("retMsg",), 1, TypeError),
        (("retExtInfo",), [], TypeError),
        (("time",), True, TypeError),
        (("time",), -1, ValueError),
        (("result",), [], TypeError),
        (("result", "category"), "inverse", ValueError),
        (("result", "nextPageCursor"), None, TypeError),
        (("result", "list"), (), TypeError),
    ],
)
def test_envelope_types_and_success_semantics_are_closed(path, value, error):
    payload = _payload()
    target = payload if len(path) == 1 else payload["result"]
    target[path[-1]] = value

    with pytest.raises(error):
        _parse(payload)


@pytest.mark.parametrize("symbol,error", [(None, TypeError), (1, TypeError), ("", ValueError),
                                             ("SOL", ValueError)])
def test_symbol_is_a_canonical_supported_asset(symbol, error):
    with pytest.raises(error, match="symbol"):
        _parse(symbol=symbol)


@pytest.mark.parametrize(
    "observed_ns,error", [(True, TypeError), (0, ValueError), (-1, ValueError)]
)
def test_observation_time_is_a_positive_integer(observed_ns, error):
    with pytest.raises(error, match="observed_ns"):
        _parse(observed_ns=observed_ns)


def test_parser_signature_exposes_only_snapshot_symbol_and_observation_time():
    signature = inspect.signature(_module().parse_bybit_positions_surface)
    assert tuple(signature.parameters) == ("payload", "symbol", "observed_ns")


def test_non_mapping_payload_is_a_type_error():
    with pytest.raises(TypeError, match="payload"):
        _parse([])
