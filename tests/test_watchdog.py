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
