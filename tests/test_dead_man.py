import pytest

import execution.cancel as cancel

NOW_MS = 1_000_000


def _schedule_recorder(calls, result=None):
    def schedule_cancel(deadline_ms):
        calls.append(deadline_ms)
        return result

    return schedule_cancel


def test_disarm_binds_none_without_calling_during_construction():
    calls, result = [], object()
    transport = cancel.bind_hl_schedule_cancel(
        now_ms=NOW_MS,
        deadline_ms=None,
        schedule_cancel=_schedule_recorder(calls, result),
    )
    assert calls == []
    assert transport() is result
    assert calls == [None]


def test_arm_accepts_the_inclusive_five_second_boundary_once():
    calls, result = [], object()
    deadline_ms = NOW_MS + 5_000
    transport = cancel.bind_hl_schedule_cancel(
        now_ms=NOW_MS,
        deadline_ms=deadline_ms,
        schedule_cancel=_schedule_recorder(calls, result),
    )
    assert calls == []
    assert transport() is result
    assert calls == [deadline_ms]


@pytest.mark.parametrize(
    ("now_ms", "deadline_ms", "error"),
    [
        (True, None, TypeError),
        ("1", NOW_MS + 5_000, TypeError),
        (0, None, ValueError),
        (-1, NOW_MS + 5_000, ValueError),
    ],
)
def test_invalid_now_preflights_before_every_business_branch(now_ms, deadline_ms, error):
    calls = []
    with pytest.raises(error, match="now_ms"):
        cancel.bind_hl_schedule_cancel(
            now_ms=now_ms,
            deadline_ms=deadline_ms,
            schedule_cancel=_schedule_recorder(calls),
        )
    assert calls == []


@pytest.mark.parametrize("deadline_ms", [True, "1005000", 1_005_000.0])
def test_arm_deadline_requires_an_integer(deadline_ms):
    calls = []
    with pytest.raises(TypeError, match="deadline_ms"):
        cancel.bind_hl_schedule_cancel(
            now_ms=NOW_MS,
            deadline_ms=deadline_ms,
            schedule_cancel=_schedule_recorder(calls),
        )
    assert calls == []


def test_arm_rejects_one_millisecond_inside_the_minimum_lead():
    calls = []
    with pytest.raises(ValueError, match="deadline_ms"):
        cancel.bind_hl_schedule_cancel(
            now_ms=NOW_MS,
            deadline_ms=NOW_MS + 4_999,
            schedule_cancel=_schedule_recorder(calls),
        )
    assert calls == []


def test_schedule_cancel_must_be_callable_after_value_preflight():
    with pytest.raises(TypeError, match="schedule_cancel"):
        cancel.bind_hl_schedule_cancel(
            now_ms=NOW_MS, deadline_ms=NOW_MS + 5_000, schedule_cancel=object()
        )


@pytest.mark.parametrize(
    ("values", "first_error"),
    [
        ({"now_ms": True, "deadline_ms": "bad"}, "now_ms"),
        ({"now_ms": NOW_MS, "deadline_ms": "bad"}, "deadline_ms"),
    ],
)
def test_value_errors_precede_a_non_callable_schedule(values, first_error):
    with pytest.raises(TypeError, match=first_error):
        cancel.bind_hl_schedule_cancel(**values, schedule_cancel=object())


def test_schedule_cancel_exception_propagates_with_identity():
    error = KeyboardInterrupt("schedule")

    def schedule_cancel(_deadline_ms):
        raise error

    transport = cancel.bind_hl_schedule_cancel(
        now_ms=NOW_MS,
        deadline_ms=NOW_MS + 5_000,
        schedule_cancel=schedule_cancel,
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        transport()
    assert caught.value is error
