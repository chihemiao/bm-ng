import asyncio
import importlib
from inspect import getdoc, getsource

import pytest

from execution.writer import WriterIdentity, WriterLease, WriterLeaseError

NOW_MS = 1_000_000


def _module():
    return importlib.import_module("ops.watchdog")


def _lease(root):
    return WriterLease.acquire(
        root,
        WriterIdentity("test-account", "watchdog", "b" * 64, "boot-one"),
        [].append,
        acquired_ns=90,
    )


def _schedule(calls, result=None):
    def schedule_cancel(deadline_ms):
        calls.append(deadline_ms)
        return result

    return schedule_cancel


def _loop_case(root, times=(NOW_MS, NOW_MS + 100)):
    events, clock, lease = [], iter(times), _lease(root)

    def stop_requested():
        events.append(("stop",))
        return sum(event[0] == "stop" for event in events) > len(times)

    def wall_ms():
        value = next(clock)
        events.append(("wall", value))
        return value

    async def wait_ms(value):
        events.append(("wait", value))

    def schedule_cancel(deadline_ms):
        events.append(("schedule", deadline_ms))

    return {
        "lease": lease,
        "stop_requested": stop_requested,
        "wall_ms": wall_ms,
        "wait_ms": wait_ms,
        "interval_ms": 1_000,
        "horizon_ms": 6_000,
        "schedule_cancel": schedule_cancel,
    }, events, lease


def _run_loop(values):
    return asyncio.run(_module().run_hl_dead_man_loop(**values))


@pytest.mark.parametrize("deadline_ms", [None, NOW_MS + 5_000])
def test_renew_step_authorizes_and_calls_arm_or_disarm_once(tmp_path, deadline_ms):
    calls, result, lease = [], object(), _lease(tmp_path)
    observed = _module().renew_hl_dead_man(
        lease=lease,
        now_ms=NOW_MS,
        deadline_ms=deadline_ms,
        schedule_cancel=_schedule(calls, result),
    )
    assert observed is result
    assert calls == [deadline_ms]
    lease.release()


def test_lease_type_preflights_without_calling_venue():
    calls = []
    with pytest.raises(TypeError, match="lease"):
        _module().renew_hl_dead_man(
            lease=object(),
            now_ms=NOW_MS,
            deadline_ms=NOW_MS + 5_000,
            schedule_cancel=_schedule(calls),
        )
    assert calls == []


@pytest.mark.parametrize(
    ("field", "bad_value", "error"),
    [
        ("now_ms", True, TypeError),
        ("now_ms", 0, ValueError),
        ("deadline_ms", "bad", TypeError),
        ("deadline_ms", NOW_MS + 4_999, ValueError),
        ("schedule_cancel", object(), TypeError),
    ],
)
def test_binder_preflight_precedes_authority_and_venue(
    tmp_path, field, bad_value, error,
):
    calls, lease = [], _lease(tmp_path)
    lease.release()
    values = {
        "lease": lease,
        "now_ms": NOW_MS,
        "deadline_ms": NOW_MS + 5_000,
        "schedule_cancel": _schedule(calls),
    }
    values[field] = bad_value
    with pytest.raises(error):
        _module().renew_hl_dead_man(**values)
    assert calls == []


@pytest.mark.parametrize("deadline_ms", [None, NOW_MS + 5_000])
def test_released_lease_blocks_arm_and_disarm_from_the_venue(tmp_path, deadline_ms):
    calls, lease = [], _lease(tmp_path)
    lease.release()
    with pytest.raises(WriterLeaseError):
        _module().renew_hl_dead_man(
            lease=lease,
            now_ms=NOW_MS,
            deadline_ms=deadline_ms,
            schedule_cancel=_schedule(calls),
        )
    assert calls == []


def test_schedule_failure_propagates_with_identity(tmp_path):
    lease, error = _lease(tmp_path), KeyboardInterrupt("schedule")

    def schedule_cancel(_deadline_ms):
        raise error

    with pytest.raises(KeyboardInterrupt) as caught:
        _module().renew_hl_dead_man(
            lease=lease,
            now_ms=NOW_MS,
            deadline_ms=NOW_MS + 5_000,
            schedule_cancel=schedule_cancel,
        )
    assert caught.value is error
    lease.release()


def test_renew_step_delegates_once_and_registers_its_omissions():
    function = _module().renew_hl_dead_man
    source = getsource(function)
    assert source.count("bind_hl_schedule_cancel(") == 1
    assert source.count('authorize("cancel_all")') == 1
    assert "HL_SCHEDULE_MIN_LEAD_MS" not in source
    documentation = getdoc(function)
    assert documentation is not None
    assert all(term in documentation for term in ("one step", "event recording", "policy/loop"))


@pytest.mark.parametrize(
    ("field", "bad_value", "error"),
    [
        ("lease", object(), TypeError),
        ("stop_requested", object(), TypeError),
        ("wall_ms", object(), TypeError),
        ("wait_ms", object(), TypeError),
        ("interval_ms", True, TypeError),
        ("interval_ms", 0, ValueError),
        ("horizon_ms", True, TypeError),
        ("horizon_ms", 0, ValueError),
        ("horizon_ms", 5_999, ValueError),
    ],
)
def test_loop_structure_preflights_before_any_callback(tmp_path, field, bad_value, error):
    values, events, lease = _loop_case(tmp_path)
    values[field] = bad_value
    with pytest.raises(error):
        _run_loop(values)
    assert events == []
    lease.release()


def test_loop_pre_stopped_returns_without_clock_wait_or_venue(tmp_path):
    values, events, lease = _loop_case(tmp_path)

    def stopped():
        events.append(("stop",))
        return True

    values["stop_requested"] = stopped
    assert _run_loop(values) is None
    assert events == [("stop",)]
    lease.release()


def test_loop_renews_twice_in_one_fixed_sequence_then_stops(tmp_path):
    values, events, lease = _loop_case(tmp_path)
    assert _run_loop(values) is None
    assert events == [
        ("stop",), ("wall", NOW_MS), ("schedule", NOW_MS + 6_000), ("wait", 1_000),
        ("stop",), ("wall", NOW_MS + 100),
        ("schedule", NOW_MS + 6_100), ("wait", 1_000), ("stop",),
    ]
    assert all(event[1] is not None for event in events if event[0] == "schedule")
    lease.release()


def test_each_round_reauthorizes_before_reaching_the_venue(tmp_path):
    values, events, lease = _loop_case(tmp_path)

    async def release_after_first(value):
        events.append(("wait", value))
        lease.release()

    values["wait_ms"] = release_after_first
    with pytest.raises(WriterLeaseError):
        _run_loop(values)
    assert [event[0] for event in events].count("schedule") == 1
    assert events[-2:] == [("stop",), ("wall", NOW_MS + 100)]


@pytest.mark.parametrize(("bad_time", "error"), [(True, TypeError), (0, ValueError)])
def test_first_wall_clock_value_must_be_a_strict_positive_integer(tmp_path, bad_time, error):
    values, events, lease = _loop_case(tmp_path, (bad_time,))
    with pytest.raises(error):
        _run_loop(values)
    assert [event[0] for event in events] == ["stop", "wall"]
    lease.release()


@pytest.mark.parametrize("second", [NOW_MS, NOW_MS - 1])
def test_later_wall_clock_values_must_strictly_increase(tmp_path, second):
    values, events, lease = _loop_case(tmp_path, (NOW_MS, second))
    with pytest.raises(ValueError, match="clock"):
        _run_loop(values)
    assert [event[0] for event in events].count("schedule") == 1
    assert [event[0] for event in events].count("wait") == 1
    lease.release()


@pytest.mark.parametrize("source", ["stop", "wall", "schedule", "wait", "cancel"])
def test_loop_failures_propagate_by_identity_without_retry_or_disarm(tmp_path, source):
    values, events, lease = _loop_case(tmp_path, (NOW_MS,))
    error = asyncio.CancelledError() if source == "cancel" else OSError(source)

    def fail(*_args):
        raise error

    async def fail_async(value):
        events.append(("wait", value))
        raise error

    if source in {"wait", "cancel"}:
        values["wait_ms"] = fail_async
    elif source == "schedule":
        def fail_schedule(deadline_ms):
            events.append(("schedule", deadline_ms))
            raise error
        values["schedule_cancel"] = fail_schedule
    else:
        values[f"{source}_ms" if source == "wall" else "stop_requested"] = fail
    with pytest.raises(BaseException) as caught:
        _run_loop(values)
    assert caught.value is error
    scheduled = [event[1] for event in events if event[0] == "schedule"]
    assert len(scheduled) <= 1 and None not in scheduled
    lease.release()


def test_loop_delegates_each_round_and_registers_its_omissions():
    source = getsource(_module().run_hl_dead_man_loop)
    assert source.count("renew_hl_dead_man(") == 1
    assert "bind_hl_schedule_cancel" not in source
    assert "deadline_ms=None" not in source
