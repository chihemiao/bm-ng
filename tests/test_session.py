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
        if payload["type"] not in {"ack", "market"}:
            raise ValueError("unknown frame")
        return DecodedFrame(kind=payload["type"], stream=payload["stream"])

    return SessionProtocol(subscription_frames=subscriptions, decode=decode)


def _uri(server) -> str:
    port = server.sockets[0].getsockname()[1]
    return f"ws://127.0.0.1:{port}"


def _session(uri, on_record, **options):
    settings = {"ping_interval": 1, "ping_timeout": 1, "ack_timeout": 1, "max_reconnects": 0}
    settings.update(options)
    return PublicSession(uri, "hyperliquid", "boot-a", _protocol(), on_record, **settings)


def test_hard_liveness_types_exclude_soft_arrival_alerts_and_cross_layer_imports() -> None:
    assert websockets.__version__ == "17.0.1"
    assert tuple(field.name for field in fields(HLLivenessEvidence)) == (
        "pong_ok",
        "subscriptions_acked",
        "file_integrity_ok",
    )
    assert tuple(field.name for field in fields(BybitLivenessEvidence)) == (
        "pong_ok",
        "sequence_ok",
    )
    assert list(inspect.signature(hl_hard_liveness).parameters) == ["evidence"]
    assert list(inspect.signature(bybit_hard_liveness).parameters) == ["evidence"]
    assert get_type_hints(hl_hard_liveness)["evidence"] is HLLivenessEvidence
    assert get_type_hints(bybit_hard_liveness)["evidence"] is BybitLivenessEvidence
    assert hl_hard_liveness(HLLivenessEvidence(True, True, True))
    assert not hl_hard_liveness(HLLivenessEvidence(True, False, True))
    assert bybit_hard_liveness(BybitLivenessEvidence(True, True))
    assert not bybit_hard_liveness(BybitLivenessEvidence(True, False))
    assert "shard" not in session_module.__dict__


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
        if len(cycles) == 1:
            await websocket.send(json.dumps({"type": "market", "stream": "first"}))
            await websocket.close()
            return
        await websocket.send(json.dumps({"type": "market", "stream": "early"}))
        await asyncio.sleep(0.01)
        await websocket.send(json.dumps({"type": "ack", "stream": HL_STREAMS[-1]}))
        await websocket.send(json.dumps({"type": "market", "stream": "after"}))
        await stop.wait()

    def on_record(record):
        records.append(record)
        if record.event_kind == "market" and json.loads(record.raw)["stream"] == "after":
            stop.set()

    async with serve(handler, "127.0.0.1", 0, ping_interval=None) as server:
        session = _session(_uri(server), on_record, max_reconnects=1)
        report = await asyncio.wait_for(session.run(stop), 2)

    assert cycles == [list(HL_STREAMS), list(HL_STREAMS)]
    markets = [
        json.loads(record.raw)["stream"] for record in records if record.event_kind == "market"
    ]
    assert markets == ["first", "after"]
    pre_ack = [record for record in records if record.payload_schema == "pre_ack_frame"]
    assert [json.loads(record.raw)["stream"] for record in pre_ack] == ["early"]
    assert report.reconnects == 1
    assert report.ack_cycles == 2


def test_real_disconnect_requires_all_venue_streams_to_reack() -> None:
    asyncio.run(_reconnect_scenario())


async def _quarantine_scenario() -> None:
    records = []
    stop = asyncio.Event()

    async def handler(websocket):
        for _ in HL_STREAMS:
            stream = json.loads(await websocket.recv())["stream"]
            await websocket.send(json.dumps({"type": "ack", "stream": stream}))
        await websocket.send(b"\xff\x00")
        await websocket.send(json.dumps({"type": "market", "stream": "valid"}))
        await stop.wait()

    def on_record(record):
        records.append(record)
        if record.event_kind == "market":
            stop.set()

    async with serve(handler, "127.0.0.1", 0, ping_interval=None) as server:
        session = _session(_uri(server), on_record)
        await asyncio.wait_for(session.run(stop), 2)

    quarantined = [record for record in records if record.payload_schema == "raw_quarantine"]
    assert len(quarantined) == 1
    assert quarantined[0].raw == b"\xff\x00"


def test_unparseable_real_frame_is_quarantined_with_original_bytes() -> None:
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
            ping_interval=0.01,
            ping_timeout=0.02,
        )
        await asyncio.wait_for(session.run(stop), 1)
    assert any(record.reason == "ping_timeout" for record in records)

    async def partial_ack(websocket):
        for index, stream in enumerate(HL_STREAMS):
            await websocket.recv()
            if index < len(HL_STREAMS) - 1:
                await websocket.send(json.dumps({"type": "ack", "stream": stream}))
        await asyncio.sleep(0.2)

    records.clear()
    async with serve(partial_ack, "127.0.0.1", 0, ping_interval=None) as ws_server:
        session = _session(_uri(ws_server), records.append, ack_timeout=0.02)
        await asyncio.wait_for(session.run(stop), 1)
    assert any(record.reason == "subscription_ack_timeout" for record in records)


def test_real_ping_and_subscription_timeouts_are_hard_liveness_failures() -> None:
    asyncio.run(_liveness_scenario())
