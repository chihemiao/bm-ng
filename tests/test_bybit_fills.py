import ast
import importlib
import inspect
import textwrap
from decimal import Decimal

import pytest

from execution.orders import make_order_intent
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


def _build(*pages, **changes):
    values = {
        "order_link_id": "client-1", "symbol": "BTC", "intended_side": "buy",
        "since_ms": int(ROW["execTime"]), "skew_allowance_ms": 0, "observed_ns": 100,
    }
    values.update(changes)
    payloads = pages or (_payload(ROW),)
    return _module().build_bybit_filled_quantity(payloads, **values)


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
    assert module.BYBIT_EXECUTION_TYPES == {
        "Trade", "AdlTrade", "Funding", "BustTrade", "Delivery", "Settle",
        "BlockTrade", "MovePosition", "FutureSpread", "CorporateAction", "UNKNOWN",
    }
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


def test_target_execution_builds_quantity_and_evidence_from_one_response_chain():
    module = _module()
    result = _build(_payload(ROW), observed_ns=321)
    assert isinstance(result, module.BybitFilledQuantity)
    assert result.quantity == Decimal("0.1")
    assert result.evidence == module.parse_bybit_fills_surface(
        (_payload(ROW),), observed_ns=321
    )


def test_bybit_filled_quantity_preserves_exact_client_identity():
    row = {**ROW, "orderLinkId": "Client-1"}
    assert _build(_payload(row), order_link_id="Client-1").client_order_id == "Client-1"


def test_canonical_symbol_exact_link_and_time_jointly_bind_quantity_not_evidence():
    rows = [
        ROW,
        {**ROW, "orderLinkId": "CLIENT-1", "execQty": "10", "execId": "wrong-link"},
        {**ROW, "symbol": "ETHUSDT", "execQty": "10", "execId": "wrong-symbol"},
        {**ROW, "execTime": str(int(ROW["execTime"]) - 1), "execQty": "10", "execId": "old"},
    ]
    result = _build(_payload(*rows))
    assert result.quantity == Decimal("0.1")
    assert result.evidence.fetched_count == 4


def test_duplicate_execution_across_pages_is_not_counted_twice():
    result = _build(_payload(ROW, cursor="next"), _payload(ROW))
    assert result.quantity == Decimal("0.1")


def test_exchange_time_window_is_inclusive_with_explicit_skew_allowance():
    at_boundary = {**ROW, "execTime": "100", "execId": "boundary"}
    too_early = {**ROW, "execTime": "99", "execId": "early"}
    result = _build(
        _payload(at_boundary, too_early), since_ms=105, skew_allowance_ms=5
    )
    assert result.quantity == Decimal("0.1")


@pytest.mark.parametrize(
    "intended_side,first_side,second_side",
    [("buy", "Buy", "Sell"), ("sell", "Sell", "Buy")],
)
def test_mixed_wire_sides_are_mapped_then_aligned_to_canonical_intent(
    intended_side, first_side, second_side
):
    rows = [
        {**ROW, "side": first_side, "execQty": "0.6", "execId": "first"},
        {**ROW, "side": second_side, "execQty": "0.2", "execId": "second"},
    ]
    assert _build(_payload(*rows), intended_side=intended_side).quantity == Decimal("0.4")


def test_net_movement_opposite_the_intent_is_unknown_not_absolute_or_zero():
    rows = [
        {**ROW, "side": "Buy", "execQty": "0.2", "execId": "first"},
        {**ROW, "side": "Sell", "execQty": "0.6", "execId": "second"},
    ]
    assert _build(_payload(*rows)).quantity is None


def test_decimal_quantities_sum_without_binary_float_and_empty_chain_is_zero():
    rows = [ROW, {**ROW, "execQty": "0.2", "execId": "second"}]
    assert _build(_payload(*rows)).quantity == Decimal("0.3")
    assert _build(_payload()).quantity == Decimal(0)


@pytest.mark.parametrize(
    "changes,error,match",
    [
        ({"order_link_id": None}, TypeError, "order_link_id"),
        ({"order_link_id": ""}, ValueError, "order_link_id"),
        ({"symbol": None}, TypeError, "symbol"), ({"symbol": "SOL"}, ValueError, "symbol"),
        ({"intended_side": "Buy"}, ValueError, "intended_side"),
        ({"since_ms": True}, TypeError, "since_ms"), ({"since_ms": -1}, ValueError, "since_ms"),
        ({"skew_allowance_ms": True}, TypeError, "skew_allowance_ms"),
        ({"skew_allowance_ms": -1}, ValueError, "skew_allowance_ms"),
    ],
)
def test_fill_quantity_boundaries_reject_ambiguous_inputs(changes, error, match):
    with pytest.raises(error, match=match):
        _build(_payload(ROW), **changes)


def test_aggregator_uses_shared_row_parser_and_has_a_narrow_signature():
    module = _module()
    dispatch = importlib.import_module("data.schema_dispatch")
    assert module.BYBIT_WIRE_SYMBOLS is dispatch.BYBIT_WIRE_SYMBOLS
    parsed = module._execution_row(ROW)
    assert parsed[2:] == (Decimal("0.1"), int(ROW["execTime"]))
    function = module.build_bybit_filled_quantity
    assert tuple(inspect.signature(function).parameters) == (
        "pages", "order_link_id", "symbol", "intended_side", "since_ms",
        "skew_allowance_ms", "observed_ns",
    )
    source = textwrap.dedent(inspect.getsource(function))
    calls = {
        node.func.id for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"parse_bybit_fills_surface", "_execution_row"} <= calls


def test_bybit_filled_quantity_requires_keyword_only_identity_value_and_evidence():
    module = _module()
    assert tuple(module.BybitFilledQuantity.__dataclass_fields__) == (
        "client_order_id", "quantity", "evidence",
    )
    evidence = _parse()
    with pytest.raises(TypeError):
        module.BybitFilledQuantity(quantity=Decimal("0.1"), evidence=evidence)
    with pytest.raises(TypeError):
        module.BybitFilledQuantity("client-1", Decimal("0.1"), evidence)


@pytest.mark.parametrize(
    "symbol,side,wire_symbol,wire_side",
    [("BTC", "buy", "BTCUSDT", "Buy"), ("ETH", "sell", "ETHUSDT", "Sell")],
)
def test_bybit_intent_assembly_passes_one_intent_identity_and_direction(
    symbol, side, wire_symbol, wire_side
):
    module = _module()
    intent = make_order_intent(
        "funding-carry", "git-deadbeef", 100, "bybit",
        symbol=symbol, side=side, quantity=Decimal("0.1"),
    )
    row = {
        **ROW, "symbol": wire_symbol, "side": wire_side,
        "orderLinkId": intent.client_order_id,
    }
    result = module.build_intent_bybit_filled_quantity(
        (_payload(row),), intent=intent, since_ms=int(ROW["execTime"]),
        skew_allowance_ms=0, observed_ns=200,
    )
    expected = module.build_bybit_filled_quantity(
        (_payload(row),), order_link_id=intent.client_order_id,
        symbol=intent.symbol, intended_side=intent.side,
        since_ms=int(ROW["execTime"]), skew_allowance_ms=0, observed_ns=200,
    )
    assert result == expected
    assert result.quantity == Decimal("0.1")


@pytest.mark.parametrize(
    "intent,error,match",
    [
        (object(), TypeError, "intent"),
        (
            make_order_intent(
                "funding-carry", "git-deadbeef", 100, "hyperliquid",
                symbol="BTC", side="buy", quantity=Decimal("0.1"),
            ),
            ValueError,
            "bybit",
        ),
    ],
)
def test_bybit_intent_assembly_owns_only_intent_and_leg_boundaries(
    intent, error, match
):
    with pytest.raises(error, match=match):
        _module().build_intent_bybit_filled_quantity(
            object(), intent=intent, since_ms=0,
            skew_allowance_ms=0, observed_ns=200,
        )


def test_bybit_intent_assembly_signature_has_no_replay_gate_and_only_calls_b1():
    function = _module().build_intent_bybit_filled_quantity
    assert tuple(inspect.signature(function).parameters) == (
        "pages", "intent", "since_ms", "skew_allowance_ms", "observed_ns",
    )
    source = textwrap.dedent(inspect.getsource(function))
    calls = {
        node.func.id for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert calls - {"isinstance", "TypeError", "ValueError"} == {
        "build_bybit_filled_quantity"
    }
    assert "replay" not in inspect.signature(function).parameters
    assert "no replay binding or freeze gate" in inspect.getdoc(function)
