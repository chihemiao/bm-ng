import asyncio
import base64
import hashlib
import inspect
import json
from dataclasses import fields
from importlib import import_module
from typing import get_type_hints

import websockets
from websockets.asyncio.server import serve

session_module = import_module("data.session")
BybitLivenessEvidence = session_module.BybitLivenessEvidence
DecodedFrame = session_module.DecodedFrame
HLLivenessEvidence = session_module.HLLivenessEvidence
PublicSession = session_module.PublicSession
SessionProtocol = session_module.SessionProtocol
bybit_hard_liveness = session_module.bybit_hard_liveness
hl_hard_liveness = session_module.hl_hard_liveness

HL_STREAMS = tuple(f"hl-{index}" for index in range(8))


def _protocol(streams=HL_STREAMS):
    subscriptions = {
        stream: json.dumps({"type": "subscribe", "stream": stream}) for stream in streams
    }

    def decode(message):
        text = message.decode() if isinstance(message, bytes) else message
        payload = json.loads(text)
        if payload["type"] == "application_pong":
            return DecodedFrame(kind="application_pong", stream=payload.get("stream"))
        if payload["type"] not in {"ack", "market"}:
            raise ValueError("unknown frame")
        return DecodedFrame(kind=payload["type"], stream=payload["stream"])

    return SessionProtocol(
        subscription_frames=subscriptions, decode=decode,
        application_ping_frame=json.dumps({"type": "application_ping"}),
    )


def _uri(server) -> str:
    port = server.sockets[0].getsockname()[1]
    return f"ws://127.0.0.1:{port}"


def _session(uri, on_record, **options):
    settings = {
        "transport_ping_interval": 1, "transport_ping_timeout": 1,
        "application_ping_interval": 1, "application_pong_timeout": 1,
        "ack_timeout": 1, "max_reconnects": 0,
    }
    settings.update(options)
    return PublicSession(uri, "hyperliquid", "boot-a", _protocol(), on_record, **settings)


def test_hard_liveness_types_exclude_soft_arrival_alerts_and_cross_layer_imports() -> None:
    assert websockets.__version__ == "17.0.1"
    assert tuple(field.name for field in fields(HLLivenessEvidence)) == (
        "transport_keepalive_ok",
        "application_pong_ok",
        "subscriptions_acked",
        "file_integrity_ok",
    )
    assert tuple(field.name for field in fields(BybitLivenessEvidence)) == (
        "transport_keepalive_ok",
        "application_pong_ok",
        "sequence_ok",
    )
    assert tuple(field.name for field in fields(SessionProtocol)) == (
        "subscription_frames", "decode", "application_ping_frame")
    assert tuple(field.name for field in fields(DecodedFrame)) == ("kind", "stream")
    assert list(inspect.signature(hl_hard_liveness).parameters) == ["evidence"]
    assert list(inspect.signature(bybit_hard_liveness).parameters) == ["evidence"]
    assert get_type_hints(hl_hard_liveness)["evidence"] is HLLivenessEvidence
    assert get_type_hints(bybit_hard_liveness)["evidence"] is BybitLivenessEvidence
    assert hl_hard_liveness(HLLivenessEvidence(False, True, True, True))
    assert not hl_hard_liveness(HLLivenessEvidence(True, False, True, True))
    assert bybit_hard_liveness(BybitLivenessEvidence(False, True, True))
    assert not bybit_hard_liveness(BybitLivenessEvidence(True, False, True))
    for verdict in (hl_hard_liveness, bybit_hard_liveness):
        source = inspect.getsource(verdict)
        assert "application_pong_ok" in source and "transport_keepalive_ok" not in source
    assert "shard" not in session_module.__dict__


async def _application_pong(websocket) -> None:
    assert json.loads(await websocket.recv()) == {"type": "application_ping"}
    await websocket.send(json.dumps({"type": "application_pong"}))


def _assert_disconnect_evidence(records, report) -> None:
    failures = [
        (index, record) for index, record in enumerate(records)
        if record.payload_schema == "liveness_failure"
    ]
    assert [record.reason for _, record in failures] == ["transport_disconnected"]
    first_market = next(index for index, record in enumerate(records)
                        if record.event_kind == "market")
    second_send = next(index for index, record in enumerate(records)
                       if record.conn_id.endswith(":2")
                       and record.payload_schema == "subscription_send")
    assert first_market < failures[0][0] < second_send
    assert report.liveness_failures == 1


async def _reconnect_scenario() -> None:
    cycles = []
    records = []
    stop = asyncio.Event()

    async def handler(websocket):
        cycle = [json.loads(await websocket.recv())["stream"] for _ in HL_STREAMS]
        cycles.append(cycle)
        acknowledged = HL_STREAMS if len(cycles) == 1 else HL_STREAMS[:-1]
        for stream in acknowledged:
            await websocket.send(json.dumps({"type": "ack", "stream": stream}))
        await _application_pong(websocket)
        if len(cycles) == 2:
            await _application_pong(websocket)
        if len(cycles) == 1:
            await websocket.send(
                json.dumps({"type": "market", "stream": HL_STREAMS[0], "value": "first"})
            )
            await websocket.close()
            return
        await websocket.send(
            json.dumps({"type": "market", "stream": HL_STREAMS[0], "value": "early"})
        )
        await asyncio.sleep(0.01)
        await websocket.send(json.dumps({"type": "ack", "stream": HL_STREAMS[-1]}))
        await websocket.send(
            json.dumps({"type": "market", "stream": HL_STREAMS[0], "value": "after"})
        )
        await stop.wait()

    def on_record(record):
        records.append(record)
        if record.event_kind == "market" and json.loads(record.raw)["value"] == "after":
            stop.set()

    async with serve(handler, "127.0.0.1", 0, ping_interval=None) as server:
        session = _session(
            _uri(server), on_record, max_reconnects=1, application_ping_interval=0.01)
        report = await asyncio.wait_for(session.run(stop), 2)

    assert cycles == [list(HL_STREAMS), list(HL_STREAMS)]
    markets = [
        json.loads(record.raw)["value"] for record in records if record.event_kind == "market"
    ]
    assert markets == ["first", "after"]
    pre_ack = [record for record in records if record.payload_schema == "pre_ack_frame"]
    assert [json.loads(record.raw)["value"] for record in pre_ack] == ["early"]
    assert report.reconnects == 1
    assert report.ack_cycles == 2
    _assert_disconnect_evidence(records, report)
    assert len([record for record in records
                if record.payload_schema == "application_heartbeat"
                and record.phase == "pong"]) == 3


def test_real_disconnect_requires_all_venue_streams_to_reack() -> None:
    asyncio.run(_reconnect_scenario())


async def _connection_refused_scenario() -> None:
    server = await asyncio.start_server(_no_pong, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    records = []
    report = await _session(f"ws://127.0.0.1:{port}", records.append).run(asyncio.Event())
    assert report.liveness_failures == 1
    assert [(record.payload_schema, record.reason) for record in records] == [
        ("liveness_failure", "transport_disconnected"),
    ]


def test_real_connection_refusal_is_explained_transport_evidence() -> None:
    asyncio.run(_connection_refused_scenario())


async def _quarantine_scenario() -> None:
    records = []
    stop = asyncio.Event()

    async def handler(websocket):
        await asyncio.sleep(0.15)
        for _ in HL_STREAMS:
            stream = json.loads(await websocket.recv())["stream"]
            await websocket.send(json.dumps({"type": "ack", "stream": stream}))
        await _application_pong(websocket)
        await websocket.send(b"\xff\x00")
        await websocket.send(json.dumps({"type": "market", "stream": HL_STREAMS[0]}))
        await stop.wait()

    def on_record(record):
        records.append(record)
        if record.event_kind == "market":
            stop.set()

    async with serve(handler, "127.0.0.1", 0, ping_interval=None) as server:
        session = _session(_uri(server), on_record, ack_timeout=0.3)
        report = await asyncio.wait_for(session.run(stop), 2)

    quarantined = [record for record in records if record.payload_schema == "raw_quarantine"]
    assert report.liveness_failures == 0
    assert len(quarantined) == 1
    assert quarantined[0].raw == b"\xff\x00"


def test_delayed_ack_and_unparseable_frame_preserve_liveness_and_bytes() -> None:
    asyncio.run(_quarantine_scenario())


async def _no_pong(reader, writer) -> None:
    request = await reader.readuntil(b"\r\n\r\n")
    key_line = next(
        line for line in request.split(b"\r\n") if line.lower().startswith(b"sec-websocket-key:")
    )
    key = key_line.split(b":", 1)[1].strip()
    accept = base64.b64encode(
        hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
    )
    writer.write(
        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        + b"Sec-WebSocket-Accept: "
        + accept
        + b"\r\n\r\n"
    )
    await writer.drain()
    await asyncio.sleep(0.2)
    writer.close()
    await writer.wait_closed()


async def _liveness_scenario() -> None:
    records = []
    stop = asyncio.Event()
    server = await asyncio.start_server(_no_pong, "127.0.0.1", 0)
    async with server:
        session = _session(
            _uri(server),
            records.append,
            transport_ping_interval=0.01,
            transport_ping_timeout=0.02,
        )
        await asyncio.wait_for(session.run(stop), 1)
    assert any(record.reason == "transport_ping_timeout" for record in records)

    async def no_application_pong(websocket):
        for stream in HL_STREAMS:
            await websocket.recv()
            await websocket.send(json.dumps({"type": "ack", "stream": stream}))
        assert json.loads(await websocket.recv()) == {"type": "application_ping"}
        await asyncio.sleep(0.1)

    records.clear()
    async with serve(no_application_pong, "127.0.0.1", 0) as ws_server:
        session = _session(
            _uri(ws_server), records.append, application_pong_timeout=0.02)
        await asyncio.wait_for(session.run(stop), 1)
    reasons = [record.reason for record in records if record.payload_schema == "liveness_failure"]
    assert "application_pong_timeout" in reasons and "transport_ping_timeout" not in reasons

    async def partial_ack(websocket):
        for index, stream in enumerate(HL_STREAMS):
            await websocket.recv()
            if index < len(HL_STREAMS) - 1:
                await websocket.send(json.dumps({"type": "ack", "stream": stream}))
        await _application_pong(websocket)
        await asyncio.sleep(0.2)

    records.clear()
    async with serve(partial_ack, "127.0.0.1", 0, ping_interval=None) as ws_server:
        session = _session(_uri(ws_server), records.append, ack_timeout=0.02)
        await asyncio.wait_for(session.run(stop), 1)
    assert any(record.reason == "subscription_ack_timeout" for record in records)


def test_real_ping_and_subscription_timeouts_are_hard_liveness_failures() -> None:
    asyncio.run(_liveness_scenario())


async def _duplicate_pong_scenario() -> None:
    records = []
    stop = asyncio.Event()

    async def handler(websocket):
        for stream in HL_STREAMS:
            await websocket.recv()
            await websocket.send(json.dumps({"type": "ack", "stream": stream}))
        await _application_pong(websocket)
        await websocket.send(json.dumps({"type": "application_pong"}))
        await websocket.send(json.dumps({"type": "application_pong", "stream": "wrong"}))
        await stop.wait()

    def record(value):
        records.append(value)
        if sum(item.payload_schema == "raw_quarantine" for item in records) == 2:
            stop.set()

    async with serve(handler, "127.0.0.1", 0) as server:
        await asyncio.wait_for(_session(_uri(server), record).run(stop), 1)
    assert [item.phase for item in records if item.payload_schema == "application_heartbeat"] == [
        "sent", "pong"]


def test_duplicate_and_malformed_application_pongs_are_quarantined() -> None:
    asyncio.run(_duplicate_pong_scenario())
