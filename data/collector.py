import asyncio
import base64
import json
import time
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from data.contracts import ContractError, bybit_update_gap, validate_envelope
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
_BYBIT_BOOK_TOPICS = frozenset(
    f"orderbook.50.{symbol}" for symbol in BYBIT_WIRE_SYMBOLS.values()
)


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    """Record mode is candidate-window provenance, not Gate 1 passage.

    Failure events stay formal for later coverage evaluation.
    """

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
    ) -> None:
        self.writer = writer
        self.boot_id = boot_id
        self.stop = stop
        self.record_mode = record_mode
        self.previous_u: dict[str, int] = {}
        self.sequence_conn_id: str | None = None
        self.sequence_topics: set[str] = set()
        self.sequence_gap = False
        self.sequence_ok = False
        self.ready_seen = {"hyperliquid": False, "bybit": False}
        self.application_pong_seen = {"hyperliquid": False, "bybit": False}
        self.failures = {"hyperliquid": set(), "bybit": set()}
        self.down = {"hyperliquid": 0, "bybit": 0}

    def record(self, venue: str, record: SessionRecord) -> None:
        self._append(venue, record)
        if venue == "bybit" and record.conn_id != self.sequence_conn_id:
            self.sequence_conn_id = record.conn_id
            self.previous_u.clear()
            self.sequence_topics.clear()
            self.sequence_gap = False
            self.sequence_ok = False
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
            if record.payload_schema in {"pre_ack_frame", "raw_frame"}:
                self._sequence(record)
            self.sequence_ok = (
                not self.sequence_gap and self.ready_seen[venue]
                and _BYBIT_BOOK_TOPICS <= self.sequence_topics
            )

    def mark_down(self, venue: str) -> None:
        self.down[venue] += 1
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
        value = json.loads(record.raw)
        topic = value.get("topic", "")
        if topic not in _BYBIT_BOOK_TOPICS:
            return
        current, kind = value["data"]["u"], value["type"]
        previous = self.previous_u.get(topic)
        missing_snapshot = kind == "delta" and topic not in self.sequence_topics
        if missing_snapshot or bybit_update_gap(previous, current, kind):
            self.sequence_gap = True
            self.sequence_ok = False
            self._append("bybit", record, schema="bybit_sequence_gap", kind="ops")
            self.stop.set()
            return
        if kind == "snapshot":
            self.sequence_topics.add(topic)
        self.previous_u[topic] = current

    def _ops(self, venue: str, schema: str, extra: dict) -> None:
        wall = time.time_ns()
        record = SessionRecord(
            "ops", schema, json.dumps(extra).encode(), None, "collector", wall, time.monotonic_ns(),
            False,
        )
        self._append(venue, record, extra=extra)

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
        self.writer.append(encoded, record.recv_wall_ns)


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


async def run_collector(config: CollectorConfig, stop: asyncio.Event) -> CollectorReport:
    writer = ShardWriter(config.root, config.boot_id)
    sink = _Sink(writer, config.boot_id, stop, config.record_mode)
    sink.config_event(config)
    hl_protocol, bybit_protocol = _protocols()
    try:
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
