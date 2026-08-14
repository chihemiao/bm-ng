import asyncio
import inspect
import json
from dataclasses import fields

import pytest

import data.collector as collector
from data.session import SessionRecord
from data.shard import ShardWriter, load_manifest

HOUR_NS = 3_600_000_000_000


def _record(schema, mono, *, conn="c1", stream=None, ready=False, raw=b"",
            reason=None, phase=None, wall=None):
    return SessionRecord("market" if schema == "raw_frame" else "ops",
        schema, raw, stream, conn, wall or HOUR_NS + mono,
        mono, ready, reason, phase)


def _sink(root, boot="boot"):
    seen = []
    return (collector._Sink(
        ShardWriter(root, boot), boot, asyncio.Event(), "trial", seen.append), seen)


def _book(coin, u, kind="snapshot"):
    return json.dumps(dict(topic=f"orderbook.50.{coin}", type=kind, data={"u": u})).encode()


def _hl_ready(sink, start, conn="c1"):
    sink.record("hyperliquid", _record("subscription_ack", start, conn=conn, ready=True))
    sink.record("hyperliquid", _record(
        "application_heartbeat", start + 1, conn=conn, ready=True, phase="pong"))


def test_liveness_snapshot_is_strict_frozen_and_keyword_only() -> None:
    kind = collector.CollectorLivenessSnapshot
    assert [(item.name, item.type) for item in fields(kind)] == [
        ("file_integrity_ok", bool), ("hl_last_verified_mono_ns", int | None),
        ("bybit_last_verified_mono_ns", int | None)]
    assert kind.__dataclass_params__.frozen and kind.__slots__
    assert all(value.kind is inspect.Parameter.KEYWORD_ONLY
               for value in inspect.signature(kind).parameters.values())


@pytest.mark.parametrize("field,value", [
    ("file_integrity_ok", 1), ("file_integrity_ok", None),
    ("hl_last_verified_mono_ns", True), ("hl_last_verified_mono_ns", 0),
    ("hl_last_verified_mono_ns", -1), ("bybit_last_verified_mono_ns", True),
    ("bybit_last_verified_mono_ns", 0), ("bybit_last_verified_mono_ns", -1)])
def test_liveness_snapshot_rejects_invalid_fields(field, value) -> None:
    values = dict(file_integrity_ok=True, hl_last_verified_mono_ns=1,
                  bybit_last_verified_mono_ns=1)
    values[field] = value
    with pytest.raises((TypeError, ValueError), match=field):
        collector.CollectorLivenessSnapshot(**values)


def test_integrity_replays_at_startup_and_every_rotation(tmp_path) -> None:
    sink, snapshots = _sink(tmp_path)
    for hour in (1, 2):
        sink.record("hyperliquid", _record(
            "subscription_send", hour, conn=f"c{hour}", wall=hour * HOUR_NS))
    healthy, healthy_snapshots = _sink(tmp_path, "healthy")
    healthy.record("hyperliquid", _record("subscription_send", 2, wall=2 * HOUR_NS))
    path = tmp_path / load_manifest(tmp_path)["files"][0]["path"]
    path.write_bytes(path.read_bytes() + b"corrupt")
    sink.record("hyperliquid", _record(
        "subscription_send", 3, conn="c3", wall=3 * HOUR_NS))
    corrupt, corrupt_snapshots = _sink(tmp_path, "corrupt")
    corrupt.record("hyperliquid", _record("subscription_send", 4, wall=4 * HOUR_NS))
    for item in (healthy, sink, corrupt):
        item.writer.close()
    assert [item.file_integrity_ok for item in snapshots] == [False, True, False]
    assert (healthy_snapshots[0].file_integrity_ok,
            corrupt_snapshots[0].file_integrity_ok) == (True, False)


def test_hl_clock_excludes_market_and_resets_on_attributed_failures(tmp_path) -> None:
    sink, snapshots = _sink(tmp_path)
    _hl_ready(sink, 1)
    assert snapshots[-1].hl_last_verified_mono_ns == 2
    count = len(snapshots)
    sink.record("hyperliquid", _record("raw_frame", 3, ready=True, stream="l2Book:BTC"))
    assert len(snapshots) == count
    sink.record("hyperliquid", _record("subscription_send", 4, conn="c2"))
    assert snapshots[-1].hl_last_verified_mono_ns is None
    for start, schema in ((5, "raw_quarantine"), (8, "liveness_failure")):
        _hl_ready(sink, start, "c2")
        sink.record("hyperliquid", _record(
            schema, start + 2, conn="c2", raw=b"bad",
            reason="application_pong_timeout"))
        assert snapshots[-1].hl_last_verified_mono_ns is None
    _hl_ready(sink, 11, "c2")
    sink.mark_down("hyperliquid")
    assert (snapshots[-1].hl_last_verified_mono_ns,
            snapshots[-1].bybit_last_verified_mono_ns) == (None, None)
    sink.writer.close()


def test_bybit_clock_requires_pong_and_contiguous_orderbook(tmp_path) -> None:
    sink, snapshots = _sink(tmp_path)
    sink.record("bybit", _record("subscription_ack", 1, ready=True))
    for mono, symbol in ((2, "BTCUSDT"), (3, "ETHUSDT")):
        sink.record("bybit", _record(
            "raw_frame", mono, ready=True, stream=f"orderbook.50.{symbol}",
            raw=_book(symbol, 10)))
    assert snapshots[-1].bybit_last_verified_mono_ns is None
    sink.record("bybit", _record("application_heartbeat", 4, ready=True, phase="pong"))
    count = len(snapshots)
    sink.record("bybit", _record("raw_frame", 5, ready=True,
                                  stream="publicTrade.BTCUSDT",
                                  raw=b'{"topic":"publicTrade.BTCUSDT"}'))
    assert snapshots[-1].bybit_last_verified_mono_ns == 4 and len(snapshots) == count
    for mono, update in ((6, 11), (7, 13)):
        sink.record("bybit", _record(
            "raw_frame", mono, ready=True, stream="orderbook.50.BTCUSDT",
            raw=_book("BTCUSDT", update, "delta")))
    assert (snapshots[-2].bybit_last_verified_mono_ns,
            snapshots[-1].bybit_last_verified_mono_ns,
            snapshots[-1].hl_last_verified_mono_ns) == (6, None, None)
    sink.writer.close()


def test_run_collector_requires_callback_and_propagates_failure(tmp_path) -> None:
    assert inspect.signature(collector.run_collector).parameters[
        "on_liveness"].default is inspect.Parameter.empty
    failure = RuntimeError("consumer stopped")

    def fail(_snapshot):
        raise failure
    config = collector.CollectorConfig(root=tmp_path, boot_id="boot")
    stop = asyncio.Event()
    with pytest.raises(RuntimeError, match="consumer stopped") as caught:
        asyncio.run(collector.run_collector(config, stop, on_liveness=fail))
    assert caught.value is failure and stop.is_set()
