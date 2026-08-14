"""Run explicitly authorized host watchdog steps."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from execution.cancel import (
    HL_SCHEDULE_MIN_LEAD_MS,
    BybitCancelScope,
    bind_bybit_cancel,
    bind_hl_schedule_cancel,
)
from execution.writer import WriterLease
from execution.writer import read_heartbeat as read_heartbeat


@dataclass(frozen=True, slots=True, kw_only=True)
class BybitCancelRequested:
    response: object


def bybit_writer_timeout(
    lock_identity: tuple[str, int] | None, heartbeat: tuple[str, int, int] | None,
    *, now_mono_ns: int, max_gap_ns: int,
) -> bool:
    for name, value in (("now_mono_ns", now_mono_ns), ("max_gap_ns", max_gap_ns)):
        if type(value) is not int:
            raise TypeError(f"{name} must be an integer")
        if value <= 0:
            raise ValueError(f"{name} must be positive")
    if lock_identity is None or heartbeat is None:
        return True
    digest, epoch = lock_identity
    observed_digest, observed_epoch, observed_ns = heartbeat
    changed = (digest, epoch) != (observed_digest, observed_epoch)
    return changed or observed_ns > now_mono_ns or now_mono_ns - observed_ns > max_gap_ns


def request_bybit_cancel_on_timeout(
    lock_identity: tuple[str, int] | None,
    heartbeat: tuple[str, int, int] | None,
    *, now_mono_ns: int, max_gap_ns: int,
    scope: BybitCancelScope, cancel_all: Callable[..., object],
) -> BybitCancelRequested | None:
    """Return awaiting_authoritative_confirmation after one request.

    A healthy writer causes no venue call. A request is never retried here and
    is not completion without authoritative order evidence.
    """
    transport = bind_bybit_cancel(scope, cancel_all)
    healthy = not bybit_writer_timeout(
        lock_identity, heartbeat, now_mono_ns=now_mono_ns, max_gap_ns=max_gap_ns,
    )
    if healthy:
        return None
    return BybitCancelRequested(response=transport())


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


async def run_hl_dead_man_loop(
    *,
    lease: WriterLease,
    stop_requested: Callable[[], bool],
    wall_ms: Callable[[], int],
    wait_ms: Callable[[int], Awaitable[None]],
    interval_ms: int,
    horizon_ms: int,
    schedule_cancel: Callable[[int | None], object],
) -> None:
    """Renew until stopped; do not retry, disarm, or record events here."""
    if not isinstance(lease, WriterLease):
        raise TypeError("lease must be WriterLease")
    if not all(map(callable, (stop_requested, wall_ms, wait_ms))):
        raise TypeError("loop callbacks must be callable")
    if type(interval_ms) is not int or type(horizon_ms) is not int:
        raise TypeError("loop timing must use integers")
    if interval_ms <= 0 or horizon_ms <= 0:
        raise ValueError("loop timing must be positive")
    if horizon_ms < interval_ms + HL_SCHEDULE_MIN_LEAD_MS:
        raise ValueError("horizon_ms leaves insufficient renewal lead")

    previous_ms = None
    while not stop_requested():
        current_ms = wall_ms()
        if type(current_ms) is not int:
            raise TypeError("wall clock must return an integer")
        if current_ms <= 0:
            raise ValueError("wall clock must be positive")
        if previous_ms is not None and current_ms <= previous_ms:
            raise ValueError("wall clock must strictly increase")
        renew_hl_dead_man(
            lease=lease,
            now_ms=current_ms,
            deadline_ms=current_ms + horizon_ms,
            schedule_cancel=schedule_cancel,
        )
        previous_ms = current_ms
        await wait_ms(interval_ms)
