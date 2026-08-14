import asyncio
import base64
import inspect
import json
from functools import partial
from importlib import import_module
from pathlib import Path

from websockets.asyncio.server import serve

from data.shard import replay_records

collector = import_module("data.collector")
schema_dispatch = import_module("data.schema_dispatch")
CollectorConfig = collector.CollectorConfig
run_collector = collector.run_collector


def _uri(server) -> str:
    return f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"


async def _subscriptions(websocket, venue: str):
    count = 8 if venue == "hyperliquid" else 6
    frames = [json.loads(await websocket.recv()) for _ in range(count)]
    for frame in frames[:-1]:
        await websocket.send(json.dumps(_ack(venue, frame)))
    await websocket.send(json.dumps(_market(venue, "early")))
    await websocket.send(json.dumps(_ack(venue, frames[-1])))
    return frames


async def _heartbeat(websocket, venue: str):
    frame = json.loads(await websocket.recv())
    expected = {"method": "ping"} if venue == "hyperliquid" else {
        "req_id": "collector-heartbeat", "op": "ping"}
    assert frame == expected
    pong = {"channel": "pong"} if venue == "hyperliquid" else {
        "success": True, "ret_msg": "pong", "op": "ping", "req_id": ""}
    await websocket.send(json.dumps(pong))


def _ack(venue: str, frame: dict) -> dict:
    if venue == "hyperliquid":
        return {"channel": "subscriptionResponse", "data": frame}
    return {"success": True, "op": "subscribe", "req_id": frame["req_id"]}


def _market(venue: str, tag: str, *, update_id: int = 40, kind: str = "snapshot") -> dict:
    if venue == "hyperliquid":
        return {"channel": "l2Book", "data": {"coin": "BTC", "tag": tag}}
    return {
        "topic": "orderbook.50.BTCUSDT",
        "type": kind,
        "data": {"u": update_id, "tag": tag},
    }


def _config(root: Path, hl_uri: str, bybit_uri: str, **changes):
    values = {
        "root": root,
        "boot_id": "trial-a",
        "hl_uri": hl_uri,
        "bybit_uri": bybit_uri,
        "transport_ping_interval": 0.05,
        "transport_ping_timeout": 0.02,
        "application_ping_interval": 1,
        "application_pong_timeout": 0.02,
        "ack_timeout": 0.05,
        "max_reconnects": 1,
        "reconnect_backoff": 0.01,
    }
    values.update(changes)
    return CollectorConfig(**values)


def _events(root: Path) -> list[dict]:
    return [json.loads(record) for record in replay_records(root)]


def test_bybit_wire_symbols_have_one_shared_closed_source() -> None:
    assert schema_dispatch.BYBIT_WIRE_SYMBOLS == {
        "BTC": "BTCUSDT",
        "ETH": "ETHUSDT",
    }
    assert collector.BYBIT_WIRE_SYMBOLS is schema_dispatch.BYBIT_WIRE_SYMBOLS


async def _normal_handler(websocket, venue: str) -> None:
    await _subscriptions(websocket, venue)
    await _heartbeat(websocket, venue)
    for index in range(20):
        await websocket.send(json.dumps(_market(venue, f"after-{index}")))
        await asyncio.sleep(0.005)
    await websocket.wait_closed()


async def _assembled_scenario(root: Path) -> None:
    stop = asyncio.Event()
    hl = partial(_normal_handler, venue="hyperliquid")
    bybit = partial(_normal_handler, venue="bybit")
    async with (
        serve(hl, "127.0.0.1", 0, ping_interval=None) as hl_server,
        serve(bybit, "127.0.0.1", 0, ping_interval=None) as bybit_server,
    ):
        task = asyncio.create_task(
            run_collector(_config(root, _uri(hl_server), _uri(bybit_server)), stop)
        )
        await asyncio.sleep(0.08)
        stop.set()
        report = await asyncio.wait_for(task, 2)

    events = _events(root)
    sends = [event for event in events if event["payload_schema"] == "subscription_send"]
    assert [
        sum(event["venue"] == venue for event in sends) for venue in ("hyperliquid", "bybit")
    ] == [8, 6]
    assert len([event for event in events if event["payload_schema"] == "subscription_ack"]) == 14
    assert {event["venue"] for event in events if event["event_kind"] == "market"} == {
        "hyperliquid",
        "bybit",
    }
    assert all(event["is_gate1_record"] is False for event in events)
    early = [event for event in events if event["payload_schema"] == "pre_ack_frame"]
    assert {event["venue"] for event in early} == {"hyperliquid", "bybit"}
    sent_frames = [json.loads(base64.b64decode(event["payload"]["raw"])) for event in sends]
    assert {frame["subscription"]["type"] for frame in sent_frames[:8]} == {
        "l2Book", "trades", "bbo", "activeAssetCtx"
    }
    assert all(frame["args"] == [frame["req_id"]] for frame in sent_frames[8:])
    config_event = next(event for event in events if event["payload_schema"] == "collector_config")
    assert config_event["payload"]["status"] == "provisional"
    assert config_event["payload"]["provisional_defaults"] == [20, 10, 20, 10, 10, 3]
    assert report.file_integrity_ok
    assert report.hl_liveness == collector.HLLivenessEvidence(True, True, True, True)
    assert report.bybit_liveness == collector.BybitLivenessEvidence(True, True, True)
    heartbeats = [event for event in events
                  if event["payload_schema"] == "application_heartbeat"]
    assert {venue: [event["payload"]["phase"] for event in heartbeats
                    if event["venue"] == venue]
            for venue in ("hyperliquid", "bybit")} == {
                "hyperliquid": ["sent", "pong"], "bybit": ["sent", "pong"]}
    sent = {event["venue"]: json.loads(base64.b64decode(event["payload"]["raw"]))
            for event in heartbeats if event["payload"]["phase"] == "sent"}
    assert sent["hyperliquid"] == {"method": "ping"} and sent["bybit"]["op"] == "ping"
    source = inspect.getsource(collector._Sink.record)
    assert 'record.phase == "pong"' in source and 'payload_schema == "subscription_send"' in source


def test_dual_venue_assembly_stop_and_verified_trial_evidence(tmp_path: Path) -> None:
    asyncio.run(_assembled_scenario(tmp_path))


async def _gap_scenario(root: Path) -> None:
    stop = asyncio.Event()

    async def bybit(websocket):
        await _subscriptions(websocket, "bybit")
        await _heartbeat(websocket, "bybit")
        await websocket.send(json.dumps(_market("bybit", "base", update_id=40)))
        trigger = _market("bybit", "gap", update_id=42, kind="delta")
        await websocket.send(json.dumps(trigger))
        await websocket.wait_closed()

    hl = partial(_normal_handler, venue="hyperliquid")
    async with (
        serve(hl, "127.0.0.1", 0, ping_interval=None) as hl_server,
        serve(bybit, "127.0.0.1", 0, ping_interval=None) as bybit_server,
    ):
        report = await asyncio.wait_for(
            run_collector(_config(root, _uri(hl_server), _uri(bybit_server)), stop), 2
        )

    events = _events(root)
    gaps = [event for event in events if event["payload_schema"] == "bybit_sequence_gap"]
    assert stop.is_set() and len(gaps) == 1
    assert json.loads(base64.b64decode(gaps[0]["payload"]["raw"]))["data"]["u"] == 42
    assert not report.bybit_liveness.sequence_ok
    assert report.file_integrity_ok


def test_bybit_sequence_gap_closes_both_venues_after_preserving_trigger(tmp_path: Path) -> None:
    asyncio.run(_gap_scenario(tmp_path))


async def _venue_down_scenario(root: Path) -> None:
    stop = asyncio.Event()
    attempts = 0
    recovered = asyncio.Event()

    async def bybit(websocket):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            await websocket.close()
            return
        await _subscriptions(websocket, "bybit")
        await _heartbeat(websocket, "bybit")
        await websocket.send(json.dumps(_market("bybit", "recovered")))
        recovered.set()
        await websocket.wait_closed()

    hl = partial(_normal_handler, venue="hyperliquid")
    async with (
        serve(hl, "127.0.0.1", 0, ping_interval=None) as hl_server,
        serve(bybit, "127.0.0.1", 0, ping_interval=None) as bybit_server,
    ):
        task = asyncio.create_task(
            run_collector(_config(root, _uri(hl_server), _uri(bybit_server)), stop)
        )
        await asyncio.wait_for(recovered.wait(), 2)
        await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, 2)

    events = _events(root)
    down = next(
        index for index, event in enumerate(events) if event["payload_schema"] == "venue_down"
    )
    recovery = next(
        index for index, event in enumerate(events) if event["payload_schema"] == "venue_recovered"
    )
    hl_markets = [
        index
        for index, event in enumerate(events)
        if event["event_kind"] == "market" and event["venue"] == "hyperliquid"
    ]
    assert min(hl_markets) < down < max(hl_markets)
    assert down < recovery
    assert events[recovery]["payload"]["failure_count"] == 0


def test_venue_down_retries_without_stopping_the_other_venue(tmp_path: Path) -> None:
    asyncio.run(_venue_down_scenario(tmp_path))
