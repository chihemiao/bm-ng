import re

import pytest

from execution.orders import (
    OrderContractError,
    ReconciliationEvidence,
    ReplayedDecisionHistory,
    decide_submission,
    make_order_intent,
    order_request_record,
    replacement_intent,
)


def _intent(**changes):
    values = {
        "strategy_id": "funding-carry",
        "strategy_version": "git-deadbeef",
        "signal_ns": 100,
        "leg": "hyperliquid",
    }
    values.update(changes)
    return make_order_intent(**values)


def _evidence(status="absent", **changes):
    values = {
        "status": status,
        "orders_ns": 101,
        "fills_ns": 102,
        "positions_ns": 103,
    }
    values.update(changes)
    return ReconciliationEvidence(**values)


def _history(intent, *, attempts=0, frozen=False):
    return ReplayedDecisionHistory(intent.client_order_id, attempts, frozen)


def _request(intent, recorded_ns=110):
    return order_request_record(
        intent, recorded_ns=recorded_ns, account_digest="a" * 64,
        lease_epoch=1, writer_instance_id="writer-one",
    )


def _decide(intent, evidence, *, request=None, history=None, now_ns=120):
    return decide_submission(
        intent,
        evidence,
        request,
        history or _history(intent),
        now_ns=now_ns,
        max_signal_age_ns=50,
        max_reconcile_attempts=3,
    )


def test_client_order_id_is_cross_venue_and_binds_the_intent() -> None:
    intent = _intent()
    assert re.fullmatch(r"0x[0-9a-f]{32}", intent.client_order_id)
    assert _intent() == intent

    variants = {
        _intent(strategy_id="other").client_order_id,
        _intent(strategy_version="git-cafebabe").client_order_id,
        _intent(signal_ns=101).client_order_id,
        _intent(leg="bybit").client_order_id,
    }
    assert intent.client_order_id not in variants
    assert len(variants) == 4


def test_request_record_must_exist_and_match_before_submit() -> None:
    intent = _intent()
    evidence = _evidence()
    assert _decide(intent, evidence) == "persist"

    request = _request(intent)
    assert request.client_order_id == intent.client_order_id
    assert request.intent_fields() == {
        "strategy_id": "funding-carry",
        "strategy_version": "git-deadbeef",
        "signal_ns": 100,
        "leg": "hyperliquid",
        "replacement_ordinal": 0,
    }
    assert _decide(intent, evidence, request=request) == "submit"

    wrong_request = _request(_intent(leg="bybit"))
    with pytest.raises(OrderContractError, match="request does not match intent"):
        _decide(intent, evidence, request=wrong_request)


def test_request_record_binds_the_writer_lease_snapshot() -> None:
    request = order_request_record(
        _intent(), recorded_ns=110, account_digest="a" * 64,
        lease_epoch=3, writer_instance_id="writer-one",
    )

    assert request.account_digest == "a" * 64
    assert request.lease_epoch == 3
    assert request.writer_instance_id == "writer-one"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"orders_ns": None}, "query failed"),
        ({"fills_ns": 100}, "not later than signal"),
        ({"positions_ns": 99}, "older than signal"),
    ],
)
def test_absence_needs_three_successful_post_signal_queries(changes, reason) -> None:
    del reason
    intent = _intent()
    assert _decide(intent, _evidence(**changes)) == "reconcile"


@pytest.mark.parametrize("status", ["pending", "unknown"])
def test_ambiguous_state_reconciles_before_staleness_and_then_freezes(status) -> None:
    intent = _intent()
    stale_now = 1_000
    assert _decide(intent, _evidence(status), now_ns=stale_now) == "reconcile"
    exhausted = _history(intent, attempts=3)
    assert _decide(intent, _evidence(status), history=exhausted, now_ns=stale_now) == "freeze"


def test_replayed_freeze_is_an_absorbing_state() -> None:
    intent = _intent()
    request = _request(intent)
    frozen = _history(intent, frozen=True)
    assert _decide(intent, _evidence(), request=request, history=frozen) == "freeze"


@pytest.mark.parametrize(
    "status", ["open", "partially_filled", "filled", "cancelled", "rejected"]
)
def test_any_known_order_state_holds_the_same_intent(status) -> None:
    intent = _intent()
    assert _decide(intent, _evidence(status), now_ns=1_000) == "hold"


def test_only_authoritative_absence_can_reject_or_submit_a_stale_signal() -> None:
    intent = _intent()
    request = _request(intent)
    assert _decide(intent, _evidence(), request=request, now_ns=150) == "submit"
    assert _decide(intent, _evidence(), request=request, now_ns=151) == "reject_stale"


def test_replacement_requires_complete_cancelled_or_rejected_evidence() -> None:
    intent = _intent()
    replacement = replacement_intent(intent, _evidence("cancelled"))
    assert replacement.replacement_ordinal == 1
    assert replacement.client_order_id != intent.client_order_id

    for status in ("pending", "unknown", "open", "partially_filled", "filled"):
        with pytest.raises(OrderContractError, match="not replaceable"):
            replacement_intent(intent, _evidence(status))
    with pytest.raises(OrderContractError, match="authoritative terminal evidence"):
        replacement_intent(intent, _evidence("rejected", fills_ns=None))


def test_structural_invalidity_fails_closed() -> None:
    intent = _intent()
    with pytest.raises(OrderContractError, match="clock moved backwards"):
        _decide(intent, _evidence(), now_ns=99)
    with pytest.raises(OrderContractError, match="history does not match intent"):
        _decide(intent, _evidence(), history=_history(_intent(leg="bybit")))
    with pytest.raises(OrderContractError, match="invalid max_reconcile_attempts"):
        decide_submission(intent, _evidence(), None, _history(intent), 120, 50, 0)
    with pytest.raises(OrderContractError, match="invalid strategy_id"):
        _intent(strategy_id="")
