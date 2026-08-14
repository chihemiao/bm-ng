from contextlib import contextmanager
from dataclasses import fields
from decimal import Decimal
from inspect import getsource

import pytest

import execution.orders as orders
import execution.submission as submission
import reconciliation.promotion as promotion
from execution.nonce import NonceAllocator, SignerFence
from execution.writer import WriterIdentity, WriterLease, WriterLeaseError
from reconciliation.kill_switch import KillSwitchDecision

META = {"strategy_id": "funding-carry", "strategy_version": "git-deadbeef", "signal_ns": 100}
WALLET = "b" * 64


def _intent(leg, quantity, *, symbol="BTC"):
    return orders.make_order_intent(
        **META, leg=leg, symbol=symbol,
        side="buy" if leg == "hyperliquid" else "sell",
        quantity=Decimal(quantity), reduce_only=True,
    )


def _plan(hl=None, bybit=None, *, bybit_symbol="BTC"):
    return orders.FlattenIntentPlan(
        **META,
        hyperliquid=None if hl is None else _intent("hyperliquid", hl),
        bybit=None if bybit is None else _intent("bybit", bybit, symbol=bybit_symbol),
    )


@pytest.mark.parametrize(("hl", "bybit", "expected"), [
    (None, None, None), ("2", None, "hyperliquid"), (None, "2", "bybit"),
    ("2", "1", "hyperliquid"), ("1", "2", "bybit"),
    ("2", "2", "hyperliquid"),
])
def test_next_flatten_intent_minimizes_residual_delta(hl, bybit, expected):
    plan = _plan(hl, bybit)
    selected = orders.next_flatten_intent(plan)
    assert selected is (None if expected is None else getattr(plan, expected))


def test_next_flatten_intent_rejects_direct_cross_symbol_plan():
    with pytest.raises(ValueError, match="symbol"):
        orders.next_flatten_intent(_plan("1", "2", bybit_symbol="ETH"))


def test_next_flatten_intent_rejects_wrong_contract_type():
    with pytest.raises(TypeError, match="FlattenIntentPlan"):
        orders.next_flatten_intent(object())


class _Unreadable:
    def __getattribute__(self, name):
        raise AssertionError(f"unselected value was read: {name}")

    def __call__(self, *args, **kwargs):
        raise AssertionError(f"unused callable was invoked: {args!r} {kwargs!r}")


@contextmanager
def _runtime(root, *, mode="flatten_only"):
    identity = WriterIdentity("test-account", "writer-one", WALLET, "boot-one")
    lease = WriterLease.acquire(root, identity, [].append, acquired_ns=90)
    initial_mode = "risk_increasing" if mode == "flatten_only" else mode
    lease._authority = lease.authority._replace(mode=initial_mode)
    if mode == "flatten_only":
        promotion.demote_kill_switch_flatten(
            lease, KillSwitchDecision("flatten_and_stop"), now_ns=91)
    fence = SignerFence.acquire(root, WALLET, "writer-one")
    allocator = NonceAllocator(
        fence, lease, account_digest="a" * 64, replayed_last=0,
        replayed_freeze_reason=None, recorder=lambda _row: None,
    )
    try:
        yield lease, allocator
    finally:
        fence.release()
        lease.release()


def _inputs(intent, hits):
    def transport(_request):
        hits.append(intent.leg)
        return f"accepted-{intent.leg}"

    return submission.PairLegSubmissionInputs(
        evidence=orders.ReconciliationEvidence("absent", 101, 102, 103), request=None,
        history=orders.ReplayedDecisionHistory(intent.client_order_id, 0, False),
        transport=transport)


def _submit(plan, hl_input, bybit_input, runtime, recorder):
    lease, allocator = runtime
    return submission.submit_flatten_step(
        plan, hl_input, bybit_input, lease=lease, allocator=allocator,
        request_recorder=recorder, now_ns=120, max_signal_age_ns=50,
        max_reconcile_attempts=3, now_ms=500, decided_ns=110,
    )


@pytest.mark.parametrize(("hl", "bybit", "selected"), [
    ("2", None, "hyperliquid"), (None, "2", "bybit"),
    ("2", "1", "hyperliquid"), ("1", "2", "bybit"),
])
def test_step_submits_only_the_selected_intent(tmp_path, hl, bybit, selected):
    plan, hits, recorded = _plan(hl, bybit), [], []
    chosen = getattr(plan, selected)
    chosen_input, unreadable = _inputs(chosen, hits), _Unreadable()
    inputs = (chosen_input, unreadable) if selected == "hyperliquid" else (unreadable, chosen_input)
    with _runtime(tmp_path) as runtime:
        outcome = _submit(plan, *inputs, runtime, recorded.append)
    assert outcome == submission.FlattenStepOutcome(
        intent=chosen, result=("persist", f"accepted-{selected}")
    )
    assert hits == [selected] and [row.leg for row in recorded] == [selected]


def test_empty_plan_has_zero_dependencies_or_side_effects(tmp_path):
    unreadable = _Unreadable()
    with _runtime(tmp_path) as runtime:
        assert _submit(_plan(), unreadable, unreadable, runtime, unreadable) is None
        assert runtime[1].last_nonce == 0


def test_selected_input_type_is_checked_without_touching_other_leg(tmp_path):
    with _runtime(tmp_path) as runtime, pytest.raises(TypeError, match="input"):
        _submit(_plan("1"), object(), _Unreadable(), runtime, [].append)


def test_plan_type_is_checked_before_any_other_input(tmp_path):
    unreadable = _Unreadable()
    with _runtime(tmp_path) as runtime, pytest.raises(TypeError, match="FlattenIntentPlan"):
        _submit(object(), unreadable, unreadable, runtime, unreadable)


def test_authorization_error_propagates_without_touching_other_leg(tmp_path):
    plan, hits, recorded = _plan("1"), [], []
    with _runtime(tmp_path, mode="cancel_only") as runtime:
        with pytest.raises(WriterLeaseError, match="not authorized"):
            _submit(plan, _inputs(plan.hyperliquid, hits), _Unreadable(), runtime, recorded.append)
    assert hits == [] and recorded == []


def test_step_outcome_contract_and_delegation_are_pinned():
    assert [field.name for field in fields(submission.FlattenStepOutcome)] == ["intent", "result"]
    contract = submission.FlattenStepOutcome
    assert contract.__dataclass_params__.frozen and contract.__slots__
    source = getsource(submission.submit_flatten_step)
    assert source.count("next_flatten_intent(") == source.count("submit_order(") == 1
