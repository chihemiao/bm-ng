import ast
import importlib
import inspect
import textwrap
from decimal import Decimal

import pytest

from reconciliation.exposure import LegPosition
from reconciliation.state import surface_is_authoritative

ROW = {"positionIdx": 0, "symbol": "BTCUSDT", "side": "Buy", "size": "0.01"}


def _module():
    return importlib.import_module("reconciliation.bybit_surface")


def test_venue_parsers_share_the_canonical_fingerprint_definition():
    hl = importlib.import_module("reconciliation.hl_surface")
    state = importlib.import_module("reconciliation.state")
    assert hl._fingerprint is state.canonical_fingerprint
    assert _module()._fingerprint is state.canonical_fingerprint


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
    return _module().parse_bybit_positions_surface(value, symbol=symbol, observed_ns=observed_ns)


def _build(payload=None, *, symbol="BTC", observed_ns=100):
    value = _payload(ROW) if payload is None else payload
    return _module().build_bybit_leg_position(value, symbol=symbol, observed_ns=observed_ns)


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
    extra = _payload()
    extra["result"]["newField"] = 1
    with pytest.raises(ValueError, match="result fields"):
        _parse(extra)


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


@pytest.mark.parametrize("side", ["Buy", "Sell"])
def test_directional_position_row_has_full_state_and_canonical_identity(side):
    state = importlib.import_module("reconciliation.state")
    row = {**ROW, "side": side, "unconsumed": "still-hashed"}
    evidence = _parse(_payload(row))

    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (1, 0, 0)
    assert evidence.entities.fingerprints == frozenset({state.canonical_fingerprint(row)})
    identity = state.canonical_fingerprint({"symbol": "BTC"})
    assert evidence.identities.fingerprints == frozenset({identity})
    assert surface_is_authoritative(evidence, now_ns=100, max_age_ns=1)


@pytest.mark.parametrize("size", ["0", "0E+2", "-0"])
def test_empty_side_is_known_only_with_exact_numeric_zero(size):
    evidence = _parse(_payload({**ROW, "side": "", "size": size}))

    assert (evidence.fetched_count, evidence.unknown_count) == (1, 0)
    assert len(evidence.entities.fingerprints) == len(evidence.identities.fingerprints) == 1


@pytest.mark.parametrize("size", ["+1", "1E+2"])
def test_positive_finite_scientific_sizes_remain_known(size):
    evidence = _parse(_payload({**ROW, "size": size}))

    assert (evidence.fetched_count, evidence.unknown_count) == (1, 0)


@pytest.mark.parametrize(
    "side,size",
    [
        ("", "1"),
        ("", "abc"),
        ("Unknown", "1"),
        ("Buy", "0"),
        ("Sell", "-1"),
        ("Buy", ""),
        ("Buy", "abc"),
        ("Buy", "NaN"),
        ("Buy", "sNaN"),
        ("Buy", "Infinity"),
        ("Buy", "-Infinity"),
    ],
)
def test_unprovable_side_and_size_combinations_are_unknown(side, size):
    evidence = _parse(_payload({**ROW, "side": side, "size": size}))

    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (1, 1, 0)
    assert evidence.entities.fingerprints == evidence.identities.fingerprints == frozenset()
    assert evidence.fetched_count == len(evidence.entities.fingerprints) + evidence.unknown_count


@pytest.mark.parametrize("size", [None, 1, 1.0, True])
def test_size_must_be_a_string(size):
    evidence = _parse(_payload({**ROW, "size": size}))
    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)


@pytest.mark.parametrize("position_idx", [1, 2, True, "0"])
def test_only_exact_integer_one_way_mode_is_known(position_idx):
    evidence = _parse(_payload({**ROW, "positionIdx": position_idx}))
    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)


@pytest.mark.parametrize("field", ["positionIdx", "symbol", "side", "size"])
def test_missing_consumed_row_field_is_unknown(field):
    row = dict(ROW)
    row.pop(field)
    evidence = _parse(_payload(row))
    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)


@pytest.mark.parametrize("row", [[], {**ROW, "symbol": "ETHUSDT"}])
def test_non_mapping_or_wrong_wire_symbol_is_unknown(row):
    evidence = _parse(_payload(row))
    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)


@pytest.mark.parametrize("second_size", ["0.01", "0.02"])
def test_duplicate_valid_symbol_keeps_first_and_marks_mismatch(second_size):
    evidence = _parse(_payload(ROW, {**ROW, "size": second_size}))

    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (2, 1, 1)
    assert len(evidence.entities.fingerprints) == len(evidence.identities.fingerprints) == 1


def test_invalid_row_does_not_claim_identity_before_a_later_valid_row():
    evidence = _parse(_payload({**ROW, "size": "bad"}, ROW))

    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (2, 1, 0)
    assert len(evidence.entities.fingerprints) == 1


def test_two_invalid_rows_do_not_invent_a_duplicate_identity():
    evidence = _parse(_payload({**ROW, "size": "bad"}, {**ROW, "side": "Other"}))

    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (2, 2, 0)


@pytest.mark.parametrize(
    "side,size,quantity",
    [("Buy", "+1" , Decimal("1")), ("Sell", "1E+2", Decimal("-1E+2")),
     ("", "-0", Decimal("0"))],
)
def test_leg_uses_bybit_side_for_signed_quantity_from_the_same_snapshot(side, size, quantity):
    payload = _payload({**ROW, "side": side, "size": size})
    leg = _build(payload, observed_ns=321)

    assert isinstance(leg, LegPosition)
    assert (leg.venue, leg.symbol, leg.signed_quantity) == ("bybit", "BTC", quantity)
    assert leg.evidence == _module().parse_bybit_positions_surface(
        payload, symbol="BTC", observed_ns=321
    )
    assert surface_is_authoritative(leg.evidence, now_ns=321, max_age_ns=1)


def test_invalid_row_then_valid_row_uses_valid_quantity_but_not_authority():
    leg = _build(_payload({**ROW, "size": "bad"}, {**ROW, "side": "Sell", "size": "2"}))

    assert leg.signed_quantity == Decimal("-2")
    assert leg.evidence.unknown_count == 1
    assert not surface_is_authoritative(leg.evidence, now_ns=100, max_age_ns=1)


def test_duplicate_valid_rows_keep_first_quantity_and_lose_authority():
    leg = _build(_payload({**ROW, "size": "1"}, {**ROW, "size": "2"}))

    assert leg.signed_quantity == Decimal("1")
    assert (leg.evidence.unknown_count, leg.evidence.mismatch_count) == (1, 1)
    assert not surface_is_authoritative(leg.evidence, now_ns=100, max_age_ns=1)


@pytest.mark.parametrize(
    "payload", [_payload(), _payload({**ROW, "size": "bad"}),
                _payload({**ROW, "symbol": "ETHUSDT"})]
)
def test_no_valid_target_returns_zero_with_non_authoritative_evidence(payload):
    leg = _build(payload)

    assert leg.signed_quantity == Decimal(0)
    assert not surface_is_authoritative(leg.evidence, now_ns=100, max_age_ns=1)


def test_leg_builder_signature_has_no_caller_controlled_venue_or_evidence():
    signature = inspect.signature(_module().build_bybit_leg_position)
    assert tuple(signature.parameters) == ("payload", "symbol", "observed_ns")


def _direct_calls(function):
    source = textwrap.dedent(inspect.getsource(function))
    return [node.func.id for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]


def test_parser_and_builder_directly_share_quantity_normalization():
    assert "_signed_position_quantity" in _direct_calls(_module()._position_row)
    builder_calls = _direct_calls(_module().build_bybit_leg_position)
    assert "_signed_position_quantity" in builder_calls
    assert "parse_bybit_positions_surface" in builder_calls


@pytest.mark.parametrize("payload", [[], {}, {"result": []}])
def test_leg_builder_propagates_position_payload_contract_errors(payload):
    error = TypeError if isinstance(payload, list) else ValueError
    with pytest.raises(error):
        _build(payload)
