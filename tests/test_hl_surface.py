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
    module = importlib.import_module("reconciliation.hl_surface")
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
