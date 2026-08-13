import importlib
import inspect

import pytest

from reconciliation.state import canonical_fingerprint, surface_is_authoritative

ROW = {
    "symbol": "BTCUSDT", "orderLinkId": "client-1", "side": "Buy",
    "execId": "execution-1", "execQty": "0.1", "execType": "Trade", "execTime": "1672282722429",
}


def _module():
    return importlib.import_module("reconciliation.bybit_surface")


def _payload(*rows, cursor=""):
    return {
        "retCode": 0, "retMsg": "OK",
        "result": {"category": "linear", "nextPageCursor": cursor, "list": list(rows)},
        "retExtInfo": {}, "time": 1672283754510,
    }


def _parse(*pages, observed_ns=100):
    values = pages or (_payload(ROW),)
    return _module().parse_bybit_fills_surface(values, observed_ns=observed_ns)


def test_documented_trade_execution_has_full_state_and_stable_execution_identity():
    row = {**ROW, "unconsumed": "still-hashed"}
    evidence = _parse(_payload(row), observed_ns=321)

    assert evidence.observed_ns == 321
    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (1, 0, 0)
    assert evidence.entities.scheme_id == "bybit.fills.state"
    assert evidence.identities.scheme_id == "bybit.fills.identity"
    assert evidence.entities.fingerprints == frozenset({canonical_fingerprint(row)})
    identity = canonical_fingerprint({"symbol": "BTCUSDT", "execId": "execution-1"})
    assert evidence.identities.fingerprints == frozenset({identity})
    assert surface_is_authoritative(evidence, now_ns=321, max_age_ns=1)


@pytest.mark.parametrize(
    "change", [
        {"symbol": "SOLUSDT"}, {"orderLinkId": 1}, {"side": "buy"},
        {"execId": ""}, {"execQty": ""}, {"execQty": "NaN"},
        {"execQty": "-0.1"}, {"execTime": "00"}, {"execTime": "١"}, {"execTime": 1},
    ])
def test_unusable_consumed_value_is_one_unknown_trade_row(change):
    evidence = _parse(_payload({**ROW, **change}))
    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)
    assert not evidence.entities.fingerprints and not evidence.identities.fingerprints


@pytest.mark.parametrize(
    "field", ["symbol", "orderLinkId", "side", "execId", "execQty", "execType", "execTime"]
)
def test_missing_consumed_trade_field_is_unknown(field):
    row = dict(ROW)
    row.pop(field)
    evidence = _parse(_payload(row))
    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)


@pytest.mark.parametrize("quantity", ["0", "-0", "1E+2"])
def test_zero_signed_zero_and_scientific_execution_quantities_are_known(quantity):
    evidence = _parse(_payload({**ROW, "execQty": quantity}))
    assert (evidence.fetched_count, evidence.unknown_count) == (1, 0)


def test_documented_non_trade_executions_are_out_of_scope_before_counting():
    module = _module()
    non_trades = module.BYBIT_EXECUTION_TYPES - {"Trade"}
    evidence = _parse(_payload(*({"execType": value} for value in non_trades)))
    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (0, 0, 0)
    assert surface_is_authoritative(evidence, now_ns=100, max_age_ns=1)


def test_undocumented_execution_type_is_unknown_not_silently_out_of_scope():
    evidence = _parse(_payload({**ROW, "execType": "NewVenueType"}))
    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)


def test_identical_execution_on_two_pages_is_deduplicated():
    evidence = _parse(_payload(ROW, cursor="next"), _payload(ROW))
    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (1, 0, 0)


def test_same_symbol_and_execution_id_with_different_state_is_a_mismatch():
    evidence = _parse(_payload(ROW, {**ROW, "execQty": "0.2"}))
    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (1, 0, 1)


def test_empty_terminal_page_is_authoritative_zero():
    evidence = _parse(_payload())
    assert evidence.page_complete is True and evidence.truncated is False
    assert surface_is_authoritative(evidence, now_ns=100, max_age_ns=1)


def test_nonempty_terminal_cursor_is_explicitly_truncated_and_incomplete():
    evidence = _parse(_payload(ROW, cursor="next"))
    assert evidence.page_complete is False and evidence.truncated is True


def test_page_after_an_empty_cursor_is_rejected_as_an_impossible_response_chain():
    with pytest.raises(ValueError, match="cursor"):
        _parse(_payload(ROW), _payload(ROW))


@pytest.mark.parametrize("pages,error", [([], ValueError), ({}, TypeError), ([[]], TypeError)])
def test_pages_are_a_nonempty_sequence_of_response_mappings(pages, error):
    with pytest.raises(error, match="pages"):
        _module().parse_bybit_fills_surface(pages, observed_ns=100)


@pytest.mark.parametrize("observed_ns,error", [(True, TypeError), (0, ValueError)])
def test_observation_time_is_a_positive_integer(observed_ns, error):
    with pytest.raises(error, match="observed_ns"):
        _parse(observed_ns=observed_ns)


def test_parser_reuses_the_exact_bybit_envelope_and_has_a_narrow_signature():
    payload = _payload(ROW)
    payload["newField"] = 1
    with pytest.raises(ValueError, match="response fields"):
        _parse(payload)
    assert tuple(inspect.signature(_module().parse_bybit_fills_surface).parameters) == (
        "pages", "observed_ns",
    )
