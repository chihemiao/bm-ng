"""Run explicitly authorized host watchdog steps."""

from collections.abc import Callable

from execution.cancel import bind_hl_schedule_cancel
from execution.writer import WriterLease


def renew_hl_dead_man(
    *,
    lease: WriterLease,
    now_ms: int,
    deadline_ms: int | None,
    schedule_cancel: Callable[[int | None], object],
) -> object:
    """Run one step only; event recording and renewal policy/loop are separate."""
    if not isinstance(lease, WriterLease):
        raise TypeError("lease must be WriterLease")
    transport = bind_hl_schedule_cancel(
        now_ms=now_ms,
        deadline_ms=deadline_ms,
        schedule_cancel=schedule_cancel,
    )
    lease.authorize("cancel_all")
    return transport()
