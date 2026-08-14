import asyncio
import base64
import json
import time
from collections import namedtuple
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from data.contracts import ContractError, validate_envelope
from data.coverage import BybitBarrier
from data.schema_dispatch import BYBIT_WIRE_SYMBOLS
from data.session import (
    BybitLivenessEvidence,
    DecodedFrame,
    HLLivenessEvidence,
    PublicSession,
    SessionProtocol,
    SessionRecord,
)
from data.shard import ShardWriter, replay_records

HL_URI = "wss://api.hyperliquid.xyz/ws"
BYBIT_URI = "wss://stream.bybit.com/v5/public/linear"


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    """Record mode is candidate-window provenance; failure events stay formal."""

    root: Path
    boot_id: str
    hl_uri: str = HL_URI
    bybit_uri: str = BYBIT_URI
    transport_ping_interval: float = 20
    transport_ping_timeout: float = 10
    application_ping_interval: float = 20
    application_pong_timeout: float = 10
    ack_timeout: float = 10
    max_reconnects: int = 3
    reconnect_backoff: float = 5
    record_mode: Literal["trial", "formal"] = "trial"

    def __post_init__(self) -> None:
        if self.record_mode not in {"trial", "formal"}:
            raise ContractError("invalid record_mode")


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectorLivenessSnapshot:
    file_integrity_ok: bool
    hl_last_verified_mono_ns: int | None
    bybit_last_verified_mono_ns: int | None

    def __post_init__(self) -> None:
        if type(self.file_integrity_ok) is not bool:
            raise TypeError("file_integrity_ok must be a boolean")
        for name in ("hl_last_verified_mono_ns", "bybit_last_verified_mono_ns"):
            value = getattr(self, name)
            if value is not None and type(value) is not int:
                raise TypeError(f"{name} must be an integer or None")
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")


CollectorReport = namedtuple(
    "CollectorReport", "file_integrity_ok hl_liveness bybit_liveness record_mode")


def _hl_decode(message: str | bytes) -> DecodedFrame:
    value = json.loads(message)
    if value.get("channel") == "pong":
        return DecodedFrame("application_pong", None)
    if value.get("channel") == "subscriptionResponse":
        subscription = value["data"]["subscription"]
        return DecodedFrame("ack", f'{subscription["type"]}:{subscription["coin"]}')
    data = value["data"]
    item = data[0] if isinstance(data, list) else data
    return DecodedFrame("market", f'{value["channel"]}:{item["coin"]}')


def _bybit_decode(message: str | bytes) -> DecodedFrame:
    value = json.loads(message)
    pong = value.get("op") == "ping" and value.get("ret_msg") == "pong"
    if value.get("success") is True and pong:
        return DecodedFrame("application_pong", None)
    if value.get("op") == "subscribe" and value.get("success") is True:
        return DecodedFrame("ack", value["req_id"])
    return DecodedFrame("market", value["topic"])


def _protocols() -> tuple[SessionProtocol, SessionProtocol]:
    hl = {}
    bybit = {}
    for coin, wire_symbol in BYBIT_WIRE_SYMBOLS.items():
        for feed in ("l2Book", "trades", "bbo", "activeAssetCtx"):
            stream = f"{feed}:{coin}"
            hl[stream] = json.dumps(
                {"method": "subscribe", "subscription": {"type": feed, "coin": coin}}
            )
        for feed in ("orderbook.50", "publicTrade", "tickers"):
            topic = f"{feed}.{wire_symbol}"
            bybit[topic] = json.dumps({"op": "subscribe", "req_id": topic, "args": [topic]})
    return (
        SessionProtocol(hl, _hl_decode, json.dumps({"method": "ping"})),
        SessionProtocol(bybit, _bybit_decode, json.dumps(
            {"req_id": "collector-heartbeat", "op": "ping"})),
    )


class _Sink:
    def __init__(
        self, writer: ShardWriter, boot_id: str, stop: asyncio.Event, record_mode: str,
        on_liveness: Callable[[CollectorLivenessSnapshot], None],
    ) -> None:
        self.writer, self.boot_id, self.stop = writer, boot_id, stop
        self.record_mode, self.on_liveness = record_mode, on_liveness
        self.bybit_barrier = BybitBarrier()
        self.sequence_ok = False
        self.ready_seen = {"hyperliquid": False, "bybit": False}
        self.application_pong_seen = {"hyperliquid": False, "bybit": False}
        self.failures = {"hyperliquid": set(), "bybit": set()}
        self.down = {"hyperliquid": 0, "bybit": 0}
        self.conn_ids = {"hyperliquid": None, "bybit": None}
        self.last_verified = {"hyperliquid": None, "bybit": None}
        self.file_integrity_ok, self._last_snapshot = (
            _replay_integrity(writer.root, record_mode), None)

    def _publish(self) -> None:
        snapshot = CollectorLivenessSnapshot(
            file_integrity_ok=self.file_integrity_ok,
            hl_last_verified_mono_ns=self.last_verified["hyperliquid"],
            bybit_last_verified_mono_ns=self.last_verified["bybit"],
        )
        if snapshot != self._last_snapshot:
            self.on_liveness(snapshot)
            self._last_snapshot = snapshot

    def _advance_network_clock(self, venue: str, record: SessionRecord) -> None:
        pong = record.payload_schema == "application_heartbeat" and record.phase == "pong"
        if venue == "hyperliquid":
            qualifying = pong or record.payload_schema == "subscription_ack" and record.ready
            hard = self.ready_seen[venue] and self.application_pong_seen[venue]
        else:
            book = record.payload_schema == "raw_frame" and (record.stream or "").startswith(
                "orderbook.50.")
            qualifying = pong or book
            hard = self.sequence_ok and self.application_pong_seen[venue]
        if qualifying and hard:
            self.last_verified[venue] = record.recv_mono_ns

    def _update_bybit(self, record: SessionRecord) -> None:
        if record.conn_id != self.bybit_barrier.conn_id:
            self.bybit_barrier.start(record.conn_id)
            self.sequence_ok = False
        if record.payload_schema in {"pre_ack_frame", "raw_frame"}:
            self._sequence(record)
        self.sequence_ok = self.ready_seen["bybit"] and self.bybit_barrier.ready

    def record(self, venue: str, record: SessionRecord) -> None:
        self._append(venue, record)
        if record.conn_id != self.conn_ids[venue]:
            self.conn_ids[venue], self.application_pong_seen[venue] = record.conn_id, False
            self.last_verified[venue] = None
        if record.payload_schema in {"liveness_failure", "raw_quarantine"}:
            self.last_verified[venue] = None
        if record.payload_schema == "liveness_failure" and record.reason:
            self.failures[venue].add(record.reason)
        if record.payload_schema == "application_heartbeat":
            self.application_pong_seen[venue] = record.phase == "pong"
        if record.payload_schema == "subscription_send":
            self.ready_seen[venue] = False
        if record.payload_schema == "subscription_ack" and record.ready:
            self.ready_seen[venue] = True
            if self.down[venue]:
                self.down[venue] = 0
                self._ops(venue, "venue_recovered", {"failure_count": 0})
        if venue == "bybit":
            self._update_bybit(record)
        self._advance_network_clock(venue, record)
        self._publish()

    def mark_down(self, venue: str) -> None:
        self.down[venue] += 1
        self.last_verified[venue] = None
        self._ops(venue, "venue_down", {"failure_count": self.down[venue]})

    def config_event(self, config: CollectorConfig) -> None:
        active = [
            config.transport_ping_interval, config.transport_ping_timeout,
            config.application_ping_interval, config.application_pong_timeout,
            config.ack_timeout, config.max_reconnects,
        ]
        extra = {
            "status": "provisional", "provisional_defaults": [20, 10, 20, 10, 10, 3],
            "record_mode": config.record_mode,
        }
        self._ops("collector", "collector_config", extra | {"active": active})

    def _sequence(self, record: SessionRecord) -> None:
        if self.bybit_barrier.observe(record.raw):
            self.sequence_ok = False
            self.last_verified["bybit"] = None
            self._append("bybit", record, schema="bybit_sequence_gap", kind="ops")
            self.stop.set()

    def _ops(self, venue: str, schema: str, extra: dict) -> None:
        wall = time.time_ns()
        record = SessionRecord(
            "ops", schema, json.dumps(extra).encode(), None, "collector", wall, time.monotonic_ns(),
            False,
        )
        self._append(venue, record, extra=extra)
        self._publish()

    def _append(
        self, venue: str, record: SessionRecord, *, schema: str | None = None,
        kind: str | None = None, extra: dict | None = None,
    ) -> None:
        payload = {
            "raw": base64.b64encode(record.raw).decode(),
            "raw_encoding": "base64", "stream": record.stream,
            "ready": record.ready, "reason": record.reason,
        }
        if record.phase is not None:
            payload["phase"] = record.phase
        payload.update(extra or {})
        event = {
            "schema_ver": 1, "event_kind": kind or record.event_kind,
            "payload_schema": schema or record.payload_schema, "venue": venue,
            "conn_id": record.conn_id, "boot_id": self.boot_id,
            "recv_wall_ns": record.recv_wall_ns, "recv_mono_ns": record.recv_mono_ns,
            "source": "live_public_ws", "payload": payload,
            "is_gate1_record": self.record_mode == "formal",
        }
        validate_envelope(event)
        encoded = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        if self.writer.append(encoded, record.recv_wall_ns):
            self.file_integrity_ok = _replay_integrity(self.writer.root, self.record_mode)


async def _supervise(
    venue: str, uri: str, protocol: SessionProtocol, config: CollectorConfig, sink: _Sink,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        session = PublicSession(
            uri, venue, config.boot_id, protocol, lambda record: sink.record(venue, record),
            transport_ping_interval=config.transport_ping_interval,
            transport_ping_timeout=config.transport_ping_timeout,
            application_ping_interval=config.application_ping_interval,
            application_pong_timeout=config.application_pong_timeout,
            ack_timeout=config.ack_timeout, max_reconnects=config.max_reconnects,
        )
        await session.run(stop)
        if stop.is_set():
            return
        sink.mark_down(venue)
        try:
            await asyncio.wait_for(stop.wait(), config.reconnect_backoff)
        except TimeoutError:
            pass


def _replay_integrity(root: Path, record_mode: str) -> bool:
    expected_marker = record_mode == "formal"
    try:
        for raw in replay_records(root):
            if validate_envelope(json.loads(raw)).get("is_gate1_record") is not expected_marker:
                raise ContractError("record mode mismatch")
    except (ContractError, OSError, json.JSONDecodeError):
        return False
    return True


async def run_collector(
    config: CollectorConfig, stop: asyncio.Event, *,
    on_liveness: Callable[[CollectorLivenessSnapshot], None]) -> CollectorReport:
    if not callable(on_liveness):
        raise TypeError("on_liveness must be callable")
    writer = ShardWriter(config.root, config.boot_id)
    sink = _Sink(writer, config.boot_id, stop, config.record_mode, on_liveness)
    hl_protocol, bybit_protocol = _protocols()
    try:
        sink.config_event(config)
        await asyncio.gather(
            _supervise("hyperliquid", config.hl_uri, hl_protocol, config, sink, stop),
            _supervise("bybit", config.bybit_uri, bybit_protocol, config, sink, stop),
        )
    finally:
        stop.set()
        writer.close()
    integrity = _replay_integrity(config.root, config.record_mode)
    hl = HLLivenessEvidence(
        "transport_ping_timeout" not in sink.failures["hyperliquid"],
        sink.application_pong_seen["hyperliquid"],
        sink.ready_seen["hyperliquid"]
        and "subscription_ack_timeout" not in sink.failures["hyperliquid"],
        integrity,
    )
    bybit = BybitLivenessEvidence(
        "transport_ping_timeout" not in sink.failures["bybit"],
        sink.application_pong_seen["bybit"], sink.sequence_ok)
    return CollectorReport(integrity, hl, bybit, config.record_mode)
