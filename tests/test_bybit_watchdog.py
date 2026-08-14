import importlib
from inspect import getdoc, getsource

import pytest

from execution.cancel import BybitCancelScope

SCOPE = BybitCancelScope(category="linear", settle_coin="USDT")


def _module():
    return importlib.import_module("ops.watchdog")


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
