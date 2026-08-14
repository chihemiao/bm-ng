import hashlib
import importlib
import json

import pytest

POSITION = {
    "coin": "ETH",
    "cumFunding": {"allTime": "514.085417", "sinceChange": "0.0", "sinceOpen": "0.0"},
    "entryPx": "2986.3",
    "leverage": {"rawUsd": "-95.059824", "type": "isolated", "value": 20},
    "liquidationPx": "2866.26936529",
    "marginUsed": "4.967826",
    "maxLeverage": 50,
    "positionValue": "100.02765",
    "returnOnEquity": "-0.0026789",
    "szi": "0.0335",
    "unrealizedPnl": "-0.0134",
}


def _parse(payload, observed_ns=100):
    module = importlib.import_module("reconciliation.hl_positions")
    return module.parse_positions_surface(payload, observed_ns=observed_ns)


def _fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def test_observed_empty_snapshot_is_complete_and_empty():
    evidence = _parse({"assetPositions": [], "time": 1})

    assert evidence.observed_ns == 100
    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (0, 0, 0)
    assert evidence.page_complete is True and evidence.truncated is False
    assert evidence.entities.fingerprints == evidence.identities.fingerprints == frozenset()


def test_documented_position_shape_has_hashed_state_and_coin_identity():
    row = {"position": POSITION, "type": "oneWay"}
    evidence = _parse({"assetPositions": [row]})

    assert evidence.fetched_count == 1
    assert evidence.entities.scheme_id == "hyperliquid.positions.state"
    assert evidence.identities.scheme_id == "hyperliquid.positions.identity"
    assert evidence.entities.scheme_version == evidence.identities.scheme_version == 1
    assert evidence.entities.fingerprints == frozenset({_fingerprint(row)})
    assert evidence.identities.fingerprints == frozenset({_fingerprint({"coin": "ETH"})})


def test_observation_time_is_carried_without_conversion():
    assert _parse({"assetPositions": []}, observed_ns=987_654_321).observed_ns == 987_654_321


def test_missing_asset_positions_is_a_top_level_schema_error():
    with pytest.raises(ValueError, match="assetPositions"):
        _parse({"time": 1})


def test_non_list_asset_positions_is_a_top_level_schema_error():
    with pytest.raises(ValueError, match="assetPositions"):
        _parse({"assetPositions": {}})


def test_non_mapping_payload_is_a_type_error():
    with pytest.raises(TypeError, match="payload"):
        _parse([])


@pytest.mark.parametrize(
    "row",
    [{}, {"position": {**POSITION, "coin": "SOL"}, "type": "oneWay"}],
)
def test_unusable_rows_are_counted_as_unknown_not_discarded(row):
    evidence = _parse({"assetPositions": [row]})

    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)
    assert evidence.entities.fingerprints == evidence.identities.fingerprints == frozenset()


@pytest.mark.parametrize(
    "row",
    [
        {"position": POSITION, "type": "hedged"},
        {"position": {**POSITION, "newField": "drift"}, "type": "oneWay"},
    ],
)
def test_unknown_position_mode_or_field_is_not_silently_absorbed(row):
    evidence = _parse({"assetPositions": [row]})

    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)
    assert evidence.entities.fingerprints == evidence.identities.fingerprints == frozenset()


@pytest.mark.parametrize("second_position", [POSITION, {**POSITION, "szi": "0.04"}])
def test_each_duplicate_coin_is_one_unknown_mismatch(second_position):
    first = {"position": POSITION, "type": "oneWay"}
    second = {"position": second_position, "type": "oneWay"}
    evidence = _parse({"assetPositions": [first, second]})

    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (2, 1, 1)
    assert len(evidence.entities.fingerprints) == len(evidence.identities.fingerprints) == 1


@pytest.mark.parametrize("observed_ns", [True, 0])
def test_observation_time_must_be_a_positive_integer(observed_ns):
    error = TypeError if observed_ns is True else ValueError
    with pytest.raises(error, match="observed_ns"):
        _parse({"assetPositions": []}, observed_ns=observed_ns)


def test_position_snapshots_pin_the_documented_no_pagination_assumption():
    evidence = _parse({"assetPositions": [{"bad": "row"}]})

    assert evidence.page_complete is True
    assert evidence.truncated is False


@pytest.mark.parametrize("szi", ["0.0335", "-0.0335", "0", "1E+2"])
def test_finite_string_signed_sizes_remain_known(szi):
    row = {"position": {**POSITION, "szi": szi}, "type": "oneWay"}
    evidence = _parse({"assetPositions": [row]})

    assert (evidence.fetched_count, evidence.unknown_count) == (1, 0)
    assert evidence.entities.fingerprints == frozenset({_fingerprint(row)})


@pytest.mark.parametrize(
    "szi", ["", "abc", "NaN", "sNaN", "Infinity", "-Infinity", 1, 1.0, None]
)
def test_unusable_signed_sizes_make_the_surface_unknown(szi):
    row = {"position": {**POSITION, "szi": szi}, "type": "oneWay"}
    evidence = _parse({"assetPositions": [row]})

    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)
    assert evidence.entities.fingerprints == evidence.identities.fingerprints == frozenset()
    assert evidence.fetched_count == len(evidence.identities.fingerprints) + evidence.unknown_count


def test_out_of_scope_coin_with_bad_size_is_only_one_unknown():
    position = {**POSITION, "coin": "SOL", "szi": "NaN"}
    evidence = _parse({"assetPositions": [{"position": position, "type": "oneWay"}]})

    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (1, 1, 0)


ORDER = {
    "coin": "BTC", "limitPx": "29792.0", "oid": 91490942,
    "side": "A", "sz": "5.0", "timestamp": 1681247412573,
}


def _parse_orders(payload, observed_ns=100):
    module = importlib.import_module("reconciliation.hl_orders")
    return module.parse_orders_surface(payload, observed_ns=observed_ns)


def test_observed_empty_orders_are_complete_and_empty():
    evidence = _parse_orders([])

    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (0, 0, 0)
    assert evidence.page_complete is True and evidence.truncated is False
    assert evidence.entities.fingerprints == evidence.identities.fingerprints == frozenset()


def test_documented_order_shape_uses_all_state_fields_and_oid_identity():
    evidence = _parse_orders([ORDER], observed_ns=321)

    assert evidence.observed_ns == 321 and evidence.fetched_count == 1
    assert (evidence.entities.scheme_id, evidence.identities.scheme_id) == (
        "hyperliquid.orders.state", "hyperliquid.orders.identity",
    )
    assert evidence.entities.fingerprints == frozenset({_fingerprint(ORDER)})
    assert evidence.identities.fingerprints == frozenset({_fingerprint({"oid": 91490942})})


def test_non_list_orders_payload_is_a_type_error():
    with pytest.raises(TypeError, match="payload"):
        _parse_orders({})


@pytest.mark.parametrize(
    "change",
    [
        {"extra": "drift"}, {"coin": "SOL"}, {"side": "long"},
        {"oid": True}, {"oid": 2**64}, {"limitPx": object()},
    ],
)
def test_unusable_orders_are_unknown_without_being_discarded(change):
    evidence = _parse_orders([{**ORDER, **change}])

    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)
    assert evidence.entities.fingerprints == evidence.identities.fingerprints == frozenset()


def test_uint64_max_order_id_remains_known():
    evidence = _parse_orders([{**ORDER, "oid": 2**64 - 1}])
    assert (evidence.fetched_count, evidence.unknown_count) == (1, 0)


def test_hl_surface_parsers_share_one_uint64_limit_object():
    common = importlib.import_module("reconciliation.hl_common")
    fills = importlib.import_module("reconciliation.hl_fills")
    orders = importlib.import_module("reconciliation.hl_orders")
    assert orders.HL_UINT64_MAX is fills.HL_UINT64_MAX is common.HL_UINT64_MAX


def test_duplicate_oid_is_one_unknown_mismatch_and_keeps_first_order():
    evidence = _parse_orders([ORDER, {**ORDER, "sz": "4.0"}])

    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (2, 1, 1)
    assert len(evidence.entities.fingerprints) == len(evidence.identities.fingerprints) == 1


def test_raw_account_order_count_drives_the_conservative_truncation_threshold():
    payload = [{**ORDER, "coin": "SOL", "oid": oid} for oid in range(1000)]
    evidence = _parse_orders(payload)

    assert evidence.fetched_count == evidence.unknown_count == 1000
    assert evidence.page_complete is False and evidence.truncated is True


@pytest.mark.parametrize("observed_ns", [True, 0])
def test_order_observation_time_must_be_a_positive_integer(observed_ns):
    error = TypeError if observed_ns is True else ValueError
    with pytest.raises(error, match="observed_ns"):
        _parse_orders([], observed_ns=observed_ns)


USDC_BALANCE = {"coin": "USDC", "token": 0, "hold": "0.0", "total": "14.625485", "entryNtl": "0.0"}
HYPE_BALANCE = {"coin": "HYPE", "token": 150, "hold": "0", "total": "2", "entryNtl": "1"}


def _spot_payload(*balances):
    return {"balances": list(balances), "tokenToAvailableAfterMaintenance": []}


def _parse_balances(payload, *, mode="unifiedAccount", observed_ns=100):
    module = importlib.import_module("reconciliation.hl_balances")
    return module.parse_balances_surface(payload, mode=mode, observed_ns=observed_ns)


def _assert_balance_count_invariant(evidence):
    assert evidence.fetched_count == len(evidence.identities.fingerprints) + evidence.unknown_count


def test_unified_usdc_balance_is_the_only_entity_in_a_multi_token_payload():
    evidence = _parse_balances(_spot_payload(HYPE_BALANCE, USDC_BALANCE), observed_ns=321)

    assert evidence.observed_ns == 321 and evidence.fetched_count == 1
    assert (evidence.unknown_count, evidence.mismatch_count) == (0, 0)
    assert evidence.page_complete is True and evidence.truncated is False
    assert (evidence.entities.scheme_id, evidence.identities.scheme_id) == (
        "hyperliquid.balances.state", "hyperliquid.balances.identity",
    )
    assert evidence.entities.fingerprints == frozenset({_fingerprint(USDC_BALANCE)})
    assert evidence.identities.fingerprints == frozenset({_fingerprint({"token": 0})})
    _assert_balance_count_invariant(evidence)


@pytest.mark.parametrize("balances", [(), (HYPE_BALANCE,)])
def test_missing_usdc_balance_cannot_establish_a_complete_surface(balances):
    evidence = _parse_balances(_spot_payload(*balances))

    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (0, 0, 0)
    assert evidence.page_complete is False and evidence.truncated is False
    assert evidence.entities.fingerprints == evidence.identities.fingerprints == frozenset()
    _assert_balance_count_invariant(evidence)


def test_unsupported_mode_does_not_interpret_any_balance_rows():
    evidence = _parse_balances(_spot_payload(USDC_BALANCE), mode="portfolioMargin")

    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (0, 0, 0)
    assert evidence.page_complete is False and evidence.truncated is False
    assert evidence.entities.fingerprints == evidence.identities.fingerprints == frozenset()
    _assert_balance_count_invariant(evidence)


@pytest.mark.parametrize("mode,error", [(None, TypeError), ("", ValueError)])
def test_balance_mode_must_be_a_nonempty_string(mode, error):
    with pytest.raises(error, match="mode"):
        _parse_balances(_spot_payload(USDC_BALANCE), mode=mode)


def test_non_mapping_balance_payload_is_a_type_error():
    with pytest.raises(TypeError, match="payload"):
        _parse_balances([])


@pytest.mark.parametrize(
    "payload",
    [
        {"balances": []},
        {"balances": [], "tokenToAvailableAfterMaintenance": [], "new": 1},
        {"balances": {}, "tokenToAvailableAfterMaintenance": []},
    ],
)
def test_balance_top_level_schema_is_exact(payload):
    with pytest.raises(ValueError, match="balance"):
        _parse_balances(payload)


@pytest.mark.parametrize(
    "row",
    [
        {key: value for key, value in USDC_BALANCE.items() if key != "hold"},
        {**USDC_BALANCE, "new": "drift"},
        {**USDC_BALANCE, "token": False},
        {**USDC_BALANCE, "token": 0.0},
        {**USDC_BALANCE, "total": object()},
    ],
)
def test_unusable_usdc_balance_rows_are_unknown_and_still_fetched(row):
    evidence = _parse_balances(_spot_payload(row))

    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)
    assert evidence.page_complete is True
    assert not evidence.entities.fingerprints and not evidence.identities.fingerprints
    _assert_balance_count_invariant(evidence)


@pytest.mark.parametrize(
    "row",
    [{**USDC_BALANCE, "coin": "USD"}, {**HYPE_BALANCE, "coin": "USDC"}],
)
def test_token_zero_and_usdc_name_must_agree(row):
    evidence = _parse_balances(_spot_payload(row))

    assert (evidence.fetched_count, evidence.unknown_count) == (1, 1)
    assert evidence.page_complete is True
    _assert_balance_count_invariant(evidence)


def test_duplicate_usdc_token_is_one_unknown_mismatch_and_keeps_first():
    evidence = _parse_balances(_spot_payload(USDC_BALANCE, {**USDC_BALANCE, "total": "2"}))

    assert (evidence.fetched_count, evidence.unknown_count, evidence.mismatch_count) == (2, 1, 1)
    assert evidence.entities.fingerprints == frozenset({_fingerprint(USDC_BALANCE)})
    assert len(evidence.identities.fingerprints) == 1
    _assert_balance_count_invariant(evidence)


@pytest.mark.parametrize("observed_ns", [True, 0])
def test_balance_observation_time_must_be_a_positive_integer(observed_ns):
    error = TypeError if observed_ns is True else ValueError
    with pytest.raises(error, match="observed_ns"):
        _parse_balances(_spot_payload(USDC_BALANCE), observed_ns=observed_ns)
