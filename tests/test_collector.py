import asyncio
import base64
import inspect
import json
from functools import partial
from importlib import import_module
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from data.shard import ShardWriter, replay_records

collector = import_module("data.collector")
schema_dispatch = import_module("data.schema_dispatch")
CollectorConfig = collector.CollectorConfig
run_collector = partial(collector.run_collector, on_liveness=repr)


def _uri(server) -> str:
    return f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"


async def _subscriptions(
    websocket, venue: str, bybit_symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
):
    count = 8 if venue == "hyperliquid" else 6
    frames = [json.loads(await websocket.recv()) for _ in range(count)]
    for frame in frames[:-1]:
        await websocket.send(json.dumps(_ack(venue, frame)))
    symbols = (None,) if venue == "hyperliquid" else bybit_symbols
    for symbol in symbols:
        await websocket.send(json.dumps(_market(venue, "early", symbol=symbol)))
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


def _market(venue: str, tag: str, *, update_id: int = 40, kind: str = "snapshot",
            symbol: str | None = "BTCUSDT") -> dict:
    if venue == "hyperliquid":
        return {"channel": "l2Book", "data": {"coin": "BTC", "tag": tag}}
    return {
        "topic": f"orderbook.50.{symbol}",
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


def test_collector_stream_counts_have_one_closed_specification() -> None:
    assert schema_dispatch.BYBIT_WIRE_SYMBOLS == {
        "BTC": "BTCUSDT",
        "ETH": "ETHUSDT",
    }
    assert collector.BYBIT_WIRE_SYMBOLS is schema_dispatch.BYBIT_WIRE_SYMBOLS
    assert [len(item.subscription_frames) for item in collector._protocols()] == [8, 6]
    assert "重连后须重确认全部 8 条 HL 流" in Path("GOAL.md").read_text()


async def _normal_handler(websocket, venue: str) -> None:
    await _subscriptions(websocket, venue)
    await _heartbeat(websocket, venue)
    for index in range(20):
        await websocket.send(json.dumps(_market(venue, f"after-{index}")))
        await asyncio.sleep(0.005)
    await websocket.wait_closed()


def _assert_mixed_mode_fails(root: Path, event: dict) -> None:
    event["is_gate1_record"] = False
    writer = ShardWriter(root.parent / "mixed", "mixed")
    writer.append(json.dumps(event).encode(), event["recv_wall_ns"])
    writer.close()
    assert not collector._replay_integrity(root.parent / "mixed", "formal")


async def _assembled_scenario(root: Path, record_mode: str | None = None) -> None:
    stop = asyncio.Event()
    hl = partial(_normal_handler, venue="hyperliquid")
    bybit = partial(_normal_handler, venue="bybit")
    async with (
        serve(hl, "127.0.0.1", 0, ping_interval=None) as hl_server,
        serve(bybit, "127.0.0.1", 0, ping_interval=None) as bybit_server):
        changes = {} if record_mode is None else {"record_mode": record_mode}
        task = asyncio.create_task(
            run_collector(_config(root, _uri(hl_server), _uri(bybit_server), **changes), stop)
        )
        await asyncio.sleep(0.08)
        stop.set()
        report = await asyncio.wait_for(task, 2)

    events = _events(root)
    expected_mode, expected_marker = record_mode or "trial", record_mode == "formal"
    sends = [event for event in events if event["payload_schema"] == "subscription_send"]
    assert [
        sum(event["venue"] == venue for event in sends) for venue in ("hyperliquid", "bybit")
    ] == [8, 6]
    assert len([event for event in events if event["payload_schema"] == "subscription_ack"]) == 14
    market_venues = {event["venue"] for event in events if event["event_kind"] == "market"}
    assert market_venues == {"hyperliquid", "bybit"}
    assert all(event["is_gate1_record"] is expected_marker for event in events)
    early = [event for event in events if event["payload_schema"] == "pre_ack_frame"]
    assert {event["venue"] for event in early} == {"hyperliquid", "bybit"}
    assert {event["payload"]["stream"] for event in early if event["venue"] == "bybit"} == {
        "orderbook.50.BTCUSDT", "orderbook.50.ETHUSDT",
    }
    sent_frames = [json.loads(base64.b64decode(event["payload"]["raw"])) for event in sends]
    assert {frame["subscription"]["type"] for frame in sent_frames[:8]} == {
        "l2Book", "trades", "bbo", "activeAssetCtx"
    }
    assert all(frame["args"] == [frame["req_id"]] for frame in sent_frames[8:])
    config_event = next(event for event in events if event["payload_schema"] == "collector_config")
    assert config_event["payload"]["status"] == "provisional"
    assert config_event["payload"]["record_mode"] == expected_mode
    assert config_event["payload"]["provisional_defaults"] == [20, 10, 20, 10, 10, 3]
    assert report.file_integrity_ok and report.record_mode == expected_mode
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
    marker_line = next(line for line in inspect.getsource(collector._Sink._append).splitlines()
                       if '"is_gate1_record"' in line)
    assert 'self.record_mode == "formal"' in marker_line and "record." not in marker_line
    assert "_replay_integrity" in inspect.getsource(collector.run_collector)

    if expected_marker:
        _assert_mixed_mode_fails(root, events[0])


def test_dual_venue_assembly_stop_and_verified_trial_evidence(tmp_path: Path) -> None:
    asyncio.run(_assembled_scenario(tmp_path))


def test_explicit_formal_mode_marks_candidate_window_without_starting_live_collection(
    tmp_path: Path,
) -> None:
    asyncio.run(_assembled_scenario(tmp_path, "formal"))


def test_record_mode_is_closed_before_file_or_network_side_effects(tmp_path: Path) -> None:
    with pytest.raises(collector.ContractError, match="record_mode"):
        CollectorConfig(root=tmp_path / "invalid", boot_id="bad", record_mode="FORMAL")
    assert not (tmp_path / "invalid").exists()
    doc = (inspect.getdoc(CollectorConfig) or "").lower()
    assert "candidate-window provenance" in doc and "failure events stay formal" in doc


async def _gap_scenario(root: Path, record_mode: str | None = None) -> None:
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
        changes = {} if record_mode is None else {"record_mode": record_mode}
        report = await asyncio.wait_for(
            run_collector(_config(root, _uri(hl_server), _uri(bybit_server), **changes), stop), 2
        )

    events = _events(root)
    expected_mode = record_mode or "trial"
    gaps = [event for event in events if event["payload_schema"] == "bybit_sequence_gap"]
    assert stop.is_set() and len(gaps) == 1
    assert json.loads(base64.b64decode(gaps[0]["payload"]["raw"]))["data"]["u"] == 42
    assert all(event["is_gate1_record"] is (expected_mode == "formal") for event in events)
    assert not report.bybit_liveness.sequence_ok
    assert report.file_integrity_ok and report.record_mode == expected_mode


def test_bybit_sequence_gap_closes_both_venues_after_preserving_trigger(tmp_path: Path) -> None:
    asyncio.run(_gap_scenario(tmp_path))


def test_formal_gap_evidence_stays_formal_for_later_coverage_failure(tmp_path: Path) -> None:
    asyncio.run(_gap_scenario(tmp_path, "formal"))


async def _sequence_scenario(root: Path, bybit_handler) -> tuple:
    stop = asyncio.Event()
    async def hl(websocket):
        await _subscriptions(websocket, "hyperliquid")
        await _heartbeat(websocket, "hyperliquid")
        await websocket.send(json.dumps(_market("hyperliquid", "steady")))
        await websocket.wait_closed()

    bybit = partial(bybit_handler, stop=stop)

    async with (
        serve(hl, "127.0.0.1", 0, ping_interval=None) as hl_server,
        serve(bybit, "127.0.0.1", 0, ping_interval=None) as bybit_server,
    ):
        config = _config(root, _uri(hl_server), _uri(bybit_server))
        report = await asyncio.wait_for(run_collector(config, stop), 2)
    return report, _events(root)


async def _incomplete_snapshot_scenario(root: Path) -> None:
    async def bybit(websocket, stop):
        await _subscriptions(websocket, "bybit", ("BTCUSDT",))
        await _heartbeat(websocket, "bybit")
        await asyncio.sleep(0.02)
        stop.set()

    report, _ = await _sequence_scenario(root, bybit)
    assert not report.bybit_liveness.sequence_ok

    async def unacked(websocket, stop):
        frames = [json.loads(await websocket.recv()) for _ in range(6)]
        for frame in frames[:-1]:
            await websocket.send(json.dumps(_ack("bybit", frame)))
        for symbol in ("BTCUSDT", "ETHUSDT"):
            await websocket.send(json.dumps(_market("bybit", "early", symbol=symbol)))
        await _heartbeat(websocket, "bybit")
        await asyncio.sleep(0.02)
        stop.set()
        await websocket.send(json.dumps(_market("bybit", "stop-wakeup")))
        await websocket.wait_closed()

    unacked_report, events = await _sequence_scenario(root / "unacked", unacked)
    assert not unacked_report.bybit_liveness.sequence_ok
    assert not any(event["payload_schema"] == "liveness_failure" for event in events)


def test_bybit_sequence_requires_both_orderbook_snapshots(tmp_path: Path) -> None:
    asyncio.run(_incomplete_snapshot_scenario(tmp_path))


async def _reconnected_snapshot_scenario(root: Path) -> None:
    attempts = 0

    async def bybit(websocket, stop):
        nonlocal attempts
        attempts += 1
        symbols = ("BTCUSDT", "ETHUSDT") if attempts == 1 else ("BTCUSDT",)
        await _subscriptions(websocket, "bybit", symbols)
        await _heartbeat(websocket, "bybit")
        if attempts == 1:
            await websocket.close()
        else:
            await asyncio.sleep(0.02)
            stop.set()

    report, _ = await _sequence_scenario(root, bybit)
    assert attempts == 2
    assert not report.bybit_liveness.sequence_ok


def test_bybit_sequence_baseline_resets_on_new_connection(tmp_path: Path) -> None:
    asyncio.run(_reconnected_snapshot_scenario(tmp_path))


async def _snapshot_reset_scenario(root: Path) -> None:
    async def bybit(websocket, stop):
        await _subscriptions(websocket, "bybit")
        await _heartbeat(websocket, "bybit")
        updates = [
            _market("bybit", "btc-41", update_id=41, kind="delta"),
            _market("bybit", "eth-41", update_id=41, kind="delta", symbol="ETHUSDT"),
            _market("bybit", "btc-reset", update_id=100),
            _market("bybit", "btc-101", update_id=101, kind="delta")]
        for update in updates:
            await websocket.send(json.dumps(update))
        await asyncio.sleep(0.02)
        stop.set()

    report, events = await _sequence_scenario(root, bybit)
    assert report.bybit_liveness.sequence_ok
    assert not any(event["payload_schema"] == "bybit_sequence_gap" for event in events)


def test_bybit_new_snapshot_resets_one_topic_without_a_gap(tmp_path: Path) -> None:
    asyncio.run(_snapshot_reset_scenario(tmp_path))


async def _delta_before_snapshot_scenario(root: Path) -> None:
    async def bybit(websocket, stop):
        await _subscriptions(websocket, "bybit", ())
        await _heartbeat(websocket, "bybit")
        await websocket.send(json.dumps(
            _market("bybit", "missing-snapshot", update_id=41, kind="delta")
        ))
        await asyncio.sleep(0.02)
        stop.set()

    report, events = await _sequence_scenario(root, bybit)
    assert not report.bybit_liveness.sequence_ok
    assert len([event for event in events
                if event["payload_schema"] == "bybit_sequence_gap"]) == 1


def test_bybit_delta_before_snapshot_is_a_hard_gap(tmp_path: Path) -> None:
    asyncio.run(_delta_before_snapshot_scenario(tmp_path))


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
