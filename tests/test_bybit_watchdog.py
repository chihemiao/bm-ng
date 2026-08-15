import asyncio
import importlib
from dataclasses import replace
from inspect import getdoc, getsource

import pytest

from execution.cancel import BybitCancelScope
from reconciliation import state
from reconciliation.state import StartupContractError
from tests.test_flatten_intent_plan import orders_evidence
from tests.test_watchdog import ACCOUNT_ID, heartbeat_owner

SCOPE = BybitCancelScope(category="linear", settle_coin="USDT")


def _module():
    return importlib.import_module("ops.watchdog")


def _run(values):
    return asyncio.run(_module().run_until_cancel_requested(**values))


def _confirm(evidence):
    return _module().confirm_bybit_cancel_completion(
        _module().BybitCancelRequested(response=None), evidence,
        now_ns=110, max_order_age_ns=10,
    )


def _loop_case(root):
    events = []

    def stop_requested():
        events.append(("stop",))
        return False

    def mono_ns():
        events.append(("mono", 200))
        return 200

    async def wait_ms(value):
        events.append(("wait", value))

    def cancel_all(**kwargs):
        events.append(("cancel", kwargs))

    return {
        "root": root, "account_id": ACCOUNT_ID, "stop_requested": stop_requested,
        "mono_ns": mono_ns, "wait_ms": wait_ms, "interval_ms": 1_000,
        "max_gap_ns": 100, "scope": SCOPE, "cancel_all": cancel_all,
    }, events


def test_one_step_binds_scope_but_calls_venue_only_after_timeout():
    calls, module = [], _module()

    def cancel_all(**kwargs):
        calls.append(kwargs)

    healthy = ("a" * 64, 1), ("a" * 64, 1, 100)
    assert module.request_bybit_cancel_on_timeout(
        *healthy, now_mono_ns=200, max_gap_ns=100, scope=SCOPE, cancel_all=cancel_all,
    ) is None
    assert calls == []
    requested = module.request_bybit_cancel_on_timeout(
        None, None, now_mono_ns=200, max_gap_ns=100, scope=SCOPE, cancel_all=cancel_all,
    )
    assert requested == module.BybitCancelRequested(response=None)
    assert calls == [{"category": "linear", "settleCoin": "USDT"}]


@pytest.mark.parametrize(("lock_identity", "heartbeat"), [
    (None, ("a" * 64, 1, 100)),
    (("a" * 64, 1), None),
    (("a" * 64, 1), ("a" * 64, 2, 100)),
    (("a" * 64, 1), ("a" * 64, 1, 201)),
])
def test_fail_closed_cases_each_request_exactly_once(lock_identity, heartbeat):
    calls = []

    def cancel_all(**kwargs):
        calls.append(kwargs)
        return "ack"

    result = _module().request_bybit_cancel_on_timeout(
        lock_identity, heartbeat, now_mono_ns=200, max_gap_ns=100,
        scope=SCOPE, cancel_all=cancel_all,
    )
    assert result == _module().BybitCancelRequested(response="ack")
    assert len(calls) == 1


@pytest.mark.parametrize(("field", "bad"), [
    ("scope", object()), ("cancel_all", object()),
])
def test_binder_preflight_runs_even_when_heartbeat_is_healthy(field, bad):
    values = {"scope": SCOPE, "cancel_all": lambda **_kwargs: None}
    values[field] = bad
    healthy = ("a" * 64, 1), ("a" * 64, 1, 100)
    with pytest.raises(TypeError):
        _module().request_bybit_cancel_on_timeout(
            *healthy, now_mono_ns=200, max_gap_ns=100, **values,
        )


def test_cancel_failure_propagates_by_identity_without_retry():
    calls, error = [], KeyboardInterrupt("cancel")

    def fail(**kwargs):
        calls.append(kwargs)
        raise error

    with pytest.raises(KeyboardInterrupt) as caught:
        _module().request_bybit_cancel_on_timeout(
            None, None, now_mono_ns=200, max_gap_ns=100, scope=SCOPE, cancel_all=fail,
        )
    assert caught.value is error
    assert len(calls) == 1


def test_one_step_delegation_and_terminal_omissions_are_machine_pinned():
    function = _module().request_bybit_cancel_on_timeout
    source, documentation = getsource(function), getdoc(function)
    assert source.count("bind_bybit_cancel(") == 1
    assert source.count("bybit_writer_timeout(") == 1
    assert "settleCoin" not in source
    assert documentation and all(term in documentation for term in (
        "awaiting_authoritative_confirmation", "order evidence", "never retried",
    ))


@pytest.mark.parametrize(("field", "bad", "error"), [
    ("root", object(), TypeError), ("account_id", None, TypeError),
    ("account_id", "", ValueError), ("stop_requested", object(), TypeError),
    ("mono_ns", object(), TypeError), ("wait_ms", object(), TypeError),
    ("interval_ms", True, TypeError), ("interval_ms", 0, ValueError),
    ("max_gap_ns", True, TypeError), ("max_gap_ns", 0, ValueError),
    ("scope", object(), TypeError), ("cancel_all", object(), TypeError),
])
def test_loop_preflights_every_structural_input_before_callbacks(tmp_path, field, bad, error):
    values, events = _loop_case(tmp_path)
    values[field] = bad
    with pytest.raises(error):
        _run(values)
    assert events == []


def test_pre_stopped_loop_has_zero_clock_wait_or_venue_calls(tmp_path):
    values, events = _loop_case(tmp_path)

    def stopped():
        events.append(("stop",))
        return True

    values["stop_requested"] = stopped
    assert _run(values) is None
    assert events == [("stop",)]


def test_real_process_healthy_rounds_then_none_response_is_terminal(tmp_path):
    process, values, events = heartbeat_owner(tmp_path), *_loop_case(tmp_path)
    times = iter((150, 200, 201))

    def mono_ns():
        value = next(times)
        events.append(("mono", value))
        return value

    values["mono_ns"] = mono_ns
    try:
        result = _run(values)
    finally:
        process.communicate("\n", timeout=5)
    assert result == _module().BybitCancelRequested(response=None)
    assert events == [
        ("stop",), ("mono", 150), ("wait", 1_000),
        ("stop",), ("mono", 200), ("wait", 1_000),
        ("stop",), ("mono", 201),
        ("cancel", {"category": "linear", "settleCoin": "USDT"}),
    ]


def test_loop_cancel_failure_propagates_without_wait_or_retry(tmp_path):
    values, events = _loop_case(tmp_path)
    error = KeyboardInterrupt("cancel")

    def fail(**kwargs):
        events.append(("cancel", kwargs))
        raise error

    values["cancel_all"] = fail
    with pytest.raises(KeyboardInterrupt) as caught:
        _run(values)
    assert caught.value is error
    assert [event[0] for event in events] == ["stop", "mono", "cancel"]


def test_loop_delegates_once_and_registers_its_terminal_boundary():
    function = _module().run_until_cancel_requested
    source, documentation = getsource(function), getdoc(function)
    assert source.count("request_bybit_cancel_on_timeout(") == 1
    assert "bybit_writer_timeout" not in source and "bind_bybit_cancel" not in source
    assert documentation and all(term in documentation for term in (
        "awaiting_authoritative_confirmation", "no further checks", "no retry",
    ))


@pytest.mark.parametrize(("present", "expected"), [(False, True), (True, False)])
def test_orders_surface_confirms_only_authoritative_empty_evidence(present, expected):
    evidence = orders_evidence("bybit", present=present)
    assert state.orders_surface_confirmed_empty(
        evidence, "bybit", now_ns=110, max_age_ns=10,
    ) is expected


@pytest.mark.parametrize("changes", [
    {"observed_ns": 99}, {"truncated": True},
    {"fetched_count": 1, "unknown_count": 1}, {"mismatch_count": 1},
])
def test_orders_surface_treats_each_untrusted_condition_as_unconfirmed(changes):
    assert not state.orders_surface_confirmed_empty(
        orders_evidence("bybit", **changes), "bybit", now_ns=110, max_age_ns=10,
    )


def test_orders_surface_rejects_wrong_scheme_and_structural_corruption():
    with pytest.raises(ValueError, match="orders"):
        state.orders_surface_confirmed_empty(
            orders_evidence("hyperliquid"), "bybit", now_ns=110, max_age_ns=10,
        )
    corrupt = replace(orders_evidence("bybit"), fetched_count=1)
    with pytest.raises(StartupContractError, match="fetched_count"):
        state.orders_surface_confirmed_empty(
            corrupt, "bybit", now_ns=110, max_age_ns=10,
        )


@pytest.mark.parametrize(("field", "bad", "error"), [
    ("evidence", object(), StartupContractError), ("venue", None, TypeError),
    ("venue", "", ValueError), ("now_ns", None, StartupContractError),
    ("now_ns", -1, StartupContractError), ("max_age_ns", True, TypeError),
    ("max_age_ns", 0, ValueError),
])
def test_orders_surface_rejects_each_caller_contract_error(field, bad, error):
    values = {
        "evidence": orders_evidence("bybit"), "venue": "bybit",
        "now_ns": 110, "max_age_ns": 10,
    }
    values[field] = bad
    with pytest.raises(error):
        state.orders_surface_confirmed_empty(**values)


@pytest.mark.parametrize(("present", "expected"), [(False, True), (True, False)])
def test_cancel_confirmation_depends_on_order_evidence_not_ack(present, expected):
    assert _confirm(orders_evidence("bybit", present=present)) is expected


def test_cancel_confirmation_rejects_missing_request_before_order_evidence():
    with pytest.raises(TypeError, match="requested"):
        _module().confirm_bybit_cancel_completion(
            object(), object(), now_ns=110, max_order_age_ns=10,
        )


def test_cancel_confirmation_has_one_delegation_and_never_reads_response():
    function = _module().confirm_bybit_cancel_completion
    source, documentation = getsource(function), getdoc(function)
    assert source.count("orders_surface_confirmed_empty(") == 1
    assert ".response" not in source
    assert documentation and all(term in documentation for term in (
        "not yet trustworthy", "never retried", "ACK alone",
    ))
