import ast
import hashlib
import importlib
import inspect
import json
import textwrap
from decimal import Decimal

import pytest

from data.replay_order import OrderBinding

FILL = {
    "closedPnl": "0.0", "coin": "BTC", "crossed": False, "dir": "Open Long",
    "hash": "0xabc", "oid": 90542681, "px": "18.435", "side": "B",
    "startPosition": "0", "sz": "1", "time": 1681222254710, "fee": "0.01",
    "feeToken": "USDC", "tid": 118906512037719,
}


def _parse_fills(pages, *, observed_ns=100, page_complete=True, truncated=False):
    module = importlib.import_module("reconciliation.hl_fills")
    return module.parse_fills_surface(
        pages, observed_ns=observed_ns, page_complete=page_complete, truncated=truncated
    )


def _build_fills(pages, **changes):
    values = {
        "coin": "BTC", "intended_side": "buy",
        "oids": frozenset({FILL["oid"]}), "since_ms": FILL["time"],
        "skew_allowance_ms": 0, "observed_ns": 100,
        "page_complete": True, "truncated": False,
    }
    values.update(changes)
    module = importlib.import_module("reconciliation.hl_fills")
    return module.build_hl_filled_quantity(pages, **values)


def _binding(*, venue="hyperliquid", client_order_id="client-1", venue_order_id="7"):
    return OrderBinding(
        venue=venue,
        client_order_id=client_order_id,
        venue_order_id=venue_order_id,
    )


def _hl_order_ids(bindings, *, client_order_id="client-1"):
    module = importlib.import_module("reconciliation.hl_fills")
    return module.hl_order_ids(bindings, client_order_id=client_order_id)


def _fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize("flags", [(True, False), (False, True)])
def test_one_empty_fill_page_carries_explicit_completeness(flags):
    evidence = _parse_fills([[]], page_complete=flags[0], truncated=flags[1])
    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (0, 0, 0)
    assert (evidence.page_complete, evidence.truncated) == flags


def test_documented_fill_hashes_full_state_and_global_trade_identity():
    evidence = _parse_fills([[FILL]], observed_ns=321)
    identity = {"time": FILL["time"], "coin": "BTC", "tid": FILL["tid"]}
    assert evidence.observed_ns == 321 and evidence.fetched_count == 1
    assert (evidence.entities.scheme_id, evidence.identities.scheme_id) == (
        "hyperliquid.fills.state", "hyperliquid.fills.identity",
    )
    assert evidence.entities.fingerprints == frozenset({_fingerprint(FILL)})
    assert evidence.identities.fingerprints == frozenset({_fingerprint(identity)})


@pytest.mark.parametrize(
    "optional",
    [{"builderFee": "0.001"}, {"liquidation": {"method": "market", "markPx": 1}},
     {"builderFee": "0.001", "liquidation": {"method": "backstop", "markPx": 1}}],
)
def test_documented_optional_fill_fields_remain_known(optional):
    evidence = _parse_fills([[{**FILL, **optional}]])
    assert (evidence.fetched_count, evidence.unknown_count) == (1, 0)


@pytest.mark.parametrize("size", ["0", "-0", "1E+2"])
def test_zero_signed_zero_and_scientific_fill_sizes_remain_known(size):
    row = {**FILL, "sz": size}
    evidence = _parse_fills([[row]])
    assert (evidence.fetched_count, evidence.unknown_count) == (1, 0)
    assert evidence.entities.fingerprints == frozenset({_fingerprint(row)})


@pytest.mark.parametrize(
    "size",
    ["", "abc", "NaN", "sNaN", "Infinity", "-Infinity", "-0.01", 1, 1.0, None],
)
def test_unusable_fill_size_is_unknown_without_changing_fetched_count(size):
    evidence = _parse_fills([[{**FILL, "sz": size}]])
    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)
    assert not evidence.entities.fingerprints and not evidence.identities.fingerprints
    assert evidence.fetched_count == len(evidence.identities.fingerprints) + evidence.unknown_count


def test_bad_coin_and_bad_size_are_one_unknown_row_not_two():
    evidence = _parse_fills([[{**FILL, "coin": "SOL", "sz": "bad"}]])
    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)


@pytest.mark.parametrize(
    "row",
    [{**FILL, "new": 1}, {key: value for key, value in FILL.items() if key != "fee"},
     {**FILL, "coin": "SOL"}, {**FILL, "tid": True}, {**FILL, "time": 1.0},
     {**FILL, "fee": object()}],
)
def test_unusable_fills_are_unknown_without_being_discarded(row):
    evidence = _parse_fills([[row]])
    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)
    assert not evidence.entities.fingerprints and not evidence.identities.fingerprints
    assert evidence.fetched_count == len(evidence.identities.fingerprints) + evidence.unknown_count


def test_identical_fill_observed_on_two_pages_is_deduplicated():
    evidence = _parse_fills([[FILL], [FILL]])
    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (1, 0, 0)
    assert len(evidence.entities.fingerprints) == len(evidence.identities.fingerprints) == 1


def test_same_fill_identity_with_different_state_is_a_mismatch():
    evidence = _parse_fills([[FILL], [{**FILL, "fee": "0.02"}]])
    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (1, 0, 1)
    assert evidence.entities.fingerprints == frozenset({_fingerprint(FILL)})


@pytest.mark.parametrize("pages,error", [([], ValueError), ({}, TypeError), ([{}], TypeError)])
def test_fill_pages_are_a_nonempty_sequence_of_lists(pages, error):
    with pytest.raises(error, match="pages"):
        _parse_fills(pages)


@pytest.mark.parametrize("field", ["page_complete", "truncated"])
def test_fill_completeness_flags_are_exact_booleans(field):
    values = {"page_complete": True, "truncated": False, field: 1}
    with pytest.raises(TypeError, match=field):
        _parse_fills([[]], **values)


@pytest.mark.parametrize("observed_ns", [True, 0])
def test_fill_observation_time_must_be_a_positive_integer(observed_ns):
    error = TypeError if observed_ns is True else ValueError
    with pytest.raises(error, match="observed_ns"):
        _parse_fills([[]], observed_ns=observed_ns)


def test_target_order_builds_quantity_and_evidence_from_one_snapshot():
    module = importlib.import_module("reconciliation.hl_fills")
    result = _build_fills([[FILL]], observed_ns=321)

    assert isinstance(result, module.HLFilledQuantity)
    assert result.quantity == Decimal("1")
    assert result.evidence == module.parse_fills_surface(
        [[FILL]], observed_ns=321, page_complete=True, truncated=False
    )


def test_coin_and_order_ids_jointly_bound_the_quantity_not_the_evidence():
    rows = [
        FILL,
        {**FILL, "coin": "ETH", "sz": "10", "tid": 2},
        {**FILL, "oid": 7, "sz": "10", "tid": 3},
    ]
    result = _build_fills([rows])

    assert result.quantity == Decimal("1")
    assert result.evidence.fetched_count == 3


def test_duplicate_fill_across_pages_is_not_counted_twice():
    assert _build_fills([[FILL], [FILL]]).quantity == Decimal("1")


def test_exchange_time_window_is_inclusive_with_explicit_skew_allowance():
    at_boundary = {**FILL, "time": 100, "tid": 1}
    too_early = {**FILL, "time": 99, "tid": 2}
    result = _build_fills(
        [[at_boundary, too_early]], since_ms=105, skew_allowance_ms=5
    )

    assert result.quantity == Decimal("1")
    assert result.evidence.fetched_count == 2


@pytest.mark.parametrize(
    "intended_side,first_side,second_side",
    [("buy", "B", "A"), ("sell", "A", "B")],
)
def test_mixed_sides_return_net_quantity_aligned_to_the_intent(
    intended_side, first_side, second_side
):
    rows = [
        {**FILL, "side": first_side, "sz": "0.6", "tid": 1},
        {**FILL, "side": second_side, "sz": "0.2", "tid": 2},
    ]
    assert _build_fills([[*rows]], intended_side=intended_side).quantity == Decimal("0.4")


def test_net_movement_opposite_the_intent_is_unknown_not_zero_or_absolute():
    rows = [
        {**FILL, "side": "B", "sz": "0.2", "tid": 1},
        {**FILL, "side": "A", "sz": "0.6", "tid": 2},
    ]
    assert _build_fills([rows]).quantity is None


def test_bad_non_target_coin_row_still_makes_whole_evidence_non_authoritative():
    result = _build_fills([[FILL, {**FILL, "coin": "SOL", "tid": 2}]])

    assert result.quantity == Decimal("1")
    assert result.evidence.unknown_count == 1


def test_empty_order_id_set_is_authoritative_zero_when_surface_is_complete():
    assert _build_fills([[FILL]], oids=frozenset()).quantity == Decimal(0)


def test_decimal_sizes_are_summed_without_binary_float_conversion():
    rows = [
        {**FILL, "sz": "0.1", "tid": 1},
        {**FILL, "sz": "0.2", "tid": 2},
    ]
    assert _build_fills([rows]).quantity == Decimal("0.3")


@pytest.mark.parametrize("change", [{"side": "buy"}, {"oid": True}, {"oid": -1}])
def test_fill_direction_and_order_id_must_match_documented_wire_values(change):
    evidence = _parse_fills([[{**FILL, **change}]])
    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)


@pytest.mark.parametrize("change", [{"coin": []}, {"side": []}])
def test_unhashable_fill_wire_values_are_unknown_not_parser_errors(change):
    assert _parse_fills([[{**FILL, **change}]]).unknown_count == 1


@pytest.mark.parametrize(
    "changes,error,match",
    [
        ({"coin": None}, TypeError, "coin"), ({"coin": "SOL"}, ValueError, "coin"),
        ({"intended_side": "B"}, ValueError, "intended_side"),
        ({"oids": {1}}, TypeError, "oids"),
        ({"oids": frozenset({True})}, TypeError, "oids"),
        ({"since_ms": True}, TypeError, "since_ms"),
        ({"since_ms": -1}, ValueError, "since_ms"),
        ({"skew_allowance_ms": True}, TypeError, "skew_allowance_ms"),
        ({"skew_allowance_ms": -1}, ValueError, "skew_allowance_ms"),
    ],
)
def test_fill_quantity_boundaries_reject_ambiguous_inputs(changes, error, match):
    with pytest.raises(error, match=match):
        _build_fills([[FILL]], **changes)


def test_fill_quantity_signature_and_shared_parser_boundary_are_pinned():
    module = importlib.import_module("reconciliation.hl_fills")
    function = module.build_hl_filled_quantity
    assert tuple(inspect.signature(function).parameters) == (
        "pages", "coin", "intended_side", "oids", "since_ms", "skew_allowance_ms",
        "observed_ns", "page_complete", "truncated",
    )
    source = textwrap.dedent(inspect.getsource(function))
    calls = [node.func.id for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert "parse_fills_surface" in calls


@pytest.mark.parametrize(
    "wire_oid,expected",
    [("0", 0), ("90542681", 90542681), (str(2**64 - 1), 2**64 - 1)],
)
def test_hl_order_ids_accept_canonical_uint64_decimal(wire_oid, expected):
    assert _hl_order_ids((_binding(venue_order_id=wire_oid),)) == frozenset({expected})


def test_hl_order_ids_return_every_distinct_binding_for_the_client():
    bindings = (
        _binding(venue_order_id="11"),
        _binding(venue_order_id="7"),
        _binding(venue_order_id="11"),
    )
    assert _hl_order_ids(bindings) == frozenset({7, 11})


@pytest.mark.parametrize(
    "wire_oid",
    ["", "00", "+1", "-1", " 1", "1 ", "1.0", "1e3", "١", str(2**64), None],
)
def test_hl_order_ids_reject_noncanonical_or_out_of_range_values(wire_oid):
    with pytest.raises(ValueError, match="venue_order_id"):
        _hl_order_ids((_binding(venue_order_id=wire_oid),))


def test_one_invalid_matching_oid_prevents_a_partial_result():
    with pytest.raises(ValueError, match="venue_order_id"):
        _hl_order_ids((_binding(venue_order_id="7"), _binding(venue_order_id="00")))


def test_unrelated_invalid_bindings_do_not_poison_the_matching_client():
    bindings = (
        _binding(venue="bybit", venue_order_id="bad"),
        _binding(client_order_id="other", venue_order_id="bad"),
        _binding(venue_order_id="7"),
    )
    assert _hl_order_ids(bindings) == frozenset({7})


def test_missing_hl_order_binding_is_unknown_not_a_known_empty_set():
    assert _hl_order_ids((_binding(venue="bybit"),)) is None


@pytest.mark.parametrize(
    "bindings,client_order_id,error",
    [
        ([_binding()], "client-1", TypeError),
        ((object(),), "client-1", TypeError),
        ((_binding(),), None, TypeError),
        ((_binding(),), "", ValueError),
    ],
)
def test_hl_order_id_boundaries_reject_ambiguous_inputs(bindings, client_order_id, error):
    with pytest.raises(error, match="bindings|client_order_id"):
        _hl_order_ids(bindings, client_order_id=client_order_id)


def test_hl_order_ids_remain_a_source_not_fill_aggregation_assembly():
    module = importlib.import_module("reconciliation.hl_fills")
    function = module.hl_order_ids
    assert tuple(inspect.signature(function).parameters) == ("bindings", "client_order_id")
    source = textwrap.dedent(inspect.getsource(function))
    calls = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "build_hl_filled_quantity" not in calls
