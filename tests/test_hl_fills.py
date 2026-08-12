import hashlib
import importlib
import json

import pytest

FILL = {
    "closedPnl": "0.0", "coin": "BTC", "crossed": False, "dir": "Open Long",
    "hash": "0xabc", "oid": 90542681, "px": "18.435", "side": "B",
    "startPosition": "0", "sz": "1", "time": 1681222254710, "fee": "0.01",
    "feeToken": "USDC", "tid": 118906512037719,
}


def _parse_fills(pages, *, observed_ns=100, page_complete=True, truncated=False):
    module = importlib.import_module("reconciliation.hl_surface")
    return module.parse_fills_surface(
        pages, observed_ns=observed_ns, page_complete=page_complete, truncated=truncated
    )


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
