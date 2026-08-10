"""Fail-closed public WebSocket sessions with venue-local subscription barriers."""

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from data.contracts import ContractError


@dataclass(frozen=True, slots=True)
class HLLivenessEvidence:
    pong_ok: bool
    subscriptions_acked: bool
    file_integrity_ok: bool


@dataclass(frozen=True, slots=True)
class BybitLivenessEvidence:
    pong_ok: bool
    sequence_ok: bool


def hl_hard_liveness(evidence: HLLivenessEvidence) -> bool:
    return evidence.pong_ok and evidence.subscriptions_acked and evidence.file_integrity_ok


def bybit_hard_liveness(evidence: BybitLivenessEvidence) -> bool:
    return evidence.pong_ok and evidence.sequence_ok


@dataclass(frozen=True, slots=True)
class DecodedFrame:
    kind: Literal["ack", "market"]
    stream: str


@dataclass(frozen=True, slots=True)
class SessionProtocol:
    subscription_frames: Mapping[str, str | bytes]
    decode: Callable[[str | bytes], DecodedFrame]


@dataclass(frozen=True, slots=True)
class SessionRecord:
    event_kind: Literal["market", "ops"]
    payload_schema: str
    raw: bytes
    stream: str | None
    conn_id: str
    recv_wall_ns: int
    recv_mono_ns: int
    ready: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SessionReport:
    reconnects: int
    ack_cycles: int
    quarantined: int
    liveness_failures: int


class _HardLiveness(RuntimeError):
    pass


class PublicSession:
    def __init__(
        self,
        uri: str,
        venue: str,
        boot_id: str,
        protocol: SessionProtocol,
        on_record: Callable[[SessionRecord], None],
        *,
        ping_interval: float = 20,
        ping_timeout: float = 10,
        ack_timeout: float = 10,
        max_reconnects: int = 3,
    ) -> None:
        subscriptions = dict(protocol.subscription_frames)
        if not uri or not venue or not boot_id or not subscriptions:
            raise ContractError("invalid session configuration")
        if min(ping_interval, ping_timeout, ack_timeout) <= 0 or max_reconnects < 0:
            raise ContractError("invalid session timeout")
        self.uri = uri
        self.venue = venue
        self.boot_id = boot_id
        self.subscriptions = subscriptions
        self.decode = protocol.decode
        self.on_record = on_record
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.ack_timeout = ack_timeout
        self.max_reconnects = max_reconnects
        self._conn_id = ""
        self._ack_cycles = 0
        self._quarantined = 0
        self._liveness_failures = 0

    async def run(self, stop: asyncio.Event) -> SessionReport:
        reconnects = 0
        connection_number = 0
        while not stop.is_set():
            connection_number += 1
            self._conn_id = f"{self.boot_id}:{connection_number}"
            try:
                await self._run_connection(stop)
            except _HardLiveness as error:
                self._liveness_failures += 1
                self._emit("ops", "liveness_failure", b"", None, False, str(error))
            except ConnectionClosed as error:
                if _is_ping_timeout(error):
                    self._liveness_failures += 1
                    self._emit("ops", "liveness_failure", b"", None, False, "ping_timeout")
            except OSError:
                pass
            if stop.is_set() or reconnects >= self.max_reconnects:
                break
            reconnects += 1
        return SessionReport(
            reconnects, self._ack_cycles, self._quarantined, self._liveness_failures
        )

    async def _run_connection(self, stop: asyncio.Event) -> None:
        pending = set(self.subscriptions)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ack_timeout
        async with connect(
            self.uri,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
            close_timeout=min(self.ping_timeout, 0.1),
            open_timeout=1,
            proxy=None,
        ) as websocket:
            for stream, frame in self.subscriptions.items():
                raw = frame.encode() if isinstance(frame, str) else frame
                self._emit("ops", "subscription_send", raw, stream, False)
                await websocket.send(frame)
            while not stop.is_set():
                timeout = min(max(0, deadline - loop.time()), 0.1) if pending else 0.1
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout)
                except TimeoutError as error:
                    if pending:
                        raise _HardLiveness("subscription_ack_timeout") from error
                    continue
                self._handle_message(message, pending)

    def _handle_message(self, message: str | bytes, pending: set[str]) -> None:
        raw = message.encode() if isinstance(message, str) else message
        try:
            decoded = self.decode(message)
            if decoded.kind not in {"ack", "market"} or decoded.stream not in self.subscriptions:
                raise ValueError("frame outside venue protocol")
        except (KeyError, TypeError, UnicodeError, ValueError):
            self._quarantined += 1
            self._emit("ops", "raw_quarantine", raw, None, not pending)
            return
        if decoded.kind == "ack":
            was_pending = decoded.stream in pending
            pending.discard(decoded.stream)
            self._emit("ops", "subscription_ack", raw, decoded.stream, not pending)
            if was_pending and not pending:
                self._ack_cycles += 1
        elif pending:
            self._emit("ops", "pre_ack_frame", raw, decoded.stream, False)
        else:
            self._emit("market", "raw_frame", raw, decoded.stream, True)

    def _emit(
        self,
        event_kind: Literal["market", "ops"],
        payload_schema: str,
        raw: bytes,
        stream: str | None,
        ready: bool,
        reason: str | None = None,
    ) -> None:
        self.on_record(
            SessionRecord(
                event_kind=event_kind,
                payload_schema=payload_schema,
                raw=raw,
                stream=stream,
                conn_id=self._conn_id,
                recv_wall_ns=time.time_ns(),
                recv_mono_ns=time.monotonic_ns(),
                ready=ready,
                reason=reason,
            )
        )


def _is_ping_timeout(error: ConnectionClosed) -> bool:
    return getattr(getattr(error, "sent", None), "reason", None) == "keepalive ping timeout"
