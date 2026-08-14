import ast
import base64
import inspect
import json
import textwrap
from pathlib import Path

import pytest

from data import collector, coverage, latency
from data.contracts import ContractError
from data.shard import ShardWriter


def _book(symbol: str, update: int, kind: str = "snapshot") -> bytes:
    payload = {"topic": f"orderbook.50.{symbol}USDT", "type": kind, "data": {"u": update}}
    return json.dumps(payload).encode()


def _event(schema: str, at: int, *, venue: str = "hyperliquid", conn: str = "h1", **extra) -> dict:
    payload = {"raw": "", "ready": False, "reason": None} | extra
    return dict(schema_ver=1, event_kind="ops", payload_schema=schema, venue=venue,
                conn_id=conn, boot_id="boot", recv_wall_ns=at, recv_mono_ns=at,
                source="live_public_ws", payload=payload, is_gate1_record=True)


def _config(at: int = 0) -> dict:
    return _event("collector_config", at, venue="collector", conn="collector", record_mode="formal")


def _snapshot(at: int, symbol: str, *, conn: str, update: int = 1,
              kind: str = "snapshot", schema: str = "pre_ack_frame") -> dict:
    event = _event(schema, at, venue="bybit", conn=conn,
                   raw=base64.b64encode(_book(symbol, update, kind)).decode())
    event["event_kind"] = "market" if schema == "raw_frame" else "ops"
    return event


def _barrier(conn, at, symbols=("BTC", "ETH"), ready=True) -> list[dict]:
    return [_event("subscription_send", at, venue="bybit", conn=conn),
            *[_snapshot(at + index, symbol, conn=conn) for index, symbol in enumerate(symbols, 1)],
            _event("application_heartbeat", at + 3, venue="bybit", conn=conn, phase="pong"),
            _event("subscription_ack", at + 4, venue="bybit", conn=conn, ready=ready)]


def _write(root: Path, *events: dict | bytes) -> None:
    writer = ShardWriter(root, "boot")
    for index, event in enumerate(events):
        raw = event if isinstance(event, bytes) else json.dumps(event).encode()
        writer.append(raw, index if isinstance(event, bytes) else event["recv_wall_ns"])
    writer.close()


def _market(venue: str, stream: str, value: dict, at: int = 1_000_000_000) -> dict:
    event = _event(
        "raw_frame", at, venue=venue, conn="wire",
        stream=stream, raw=base64.b64encode(json.dumps(value).encode()).decode(),
    )
    event["event_kind"] = "market"
    return event


@pytest.mark.parametrize(("venue", "stream", "value", "expected"), [
    ("hyperliquid", "l2Book:BTC",
     {"channel": "l2Book", "data": {"coin": "BTC", "time": 11}}, (11,)),
    ("hyperliquid", "trades:ETH",
     {"channel": "trades", "data": [{"coin": "ETH", "time": 12}]}, (12,)),
    ("hyperliquid", "bbo:BTC",
     {"channel": "bbo", "data": {"coin": "BTC", "time": 13}}, (13,)),
    ("bybit", "orderbook.50.BTCUSDT",
     {"topic": "orderbook.50.BTCUSDT", "ts": 90, "cts": 14,
      "data": {"s": "BTCUSDT"}}, (14,)),
    ("bybit", "publicTrade.ETHUSDT",
     {"topic": "publicTrade.ETHUSDT", "ts": 91,
      "data": [{"s": "ETHUSDT", "T": 15}]}, (15,)),
    ("bybit", "tickers.BTCUSDT",
     {"topic": "tickers.BTCUSDT", "ts": 16,
      "data": {"symbol": "BTCUSDT"}}, (16,)),
])
def test_raw_latency_samples_use_channel_authoritative_exchange_times(
    venue: str, stream: str, value: dict, expected: tuple[int, ...],
) -> None:
    assert latency.raw_latency_samples(_market(venue, stream, value)) == tuple(
        (stream, timestamp) for timestamp in expected
    )


def test_raw_latency_samples_preserve_all_trades_and_allow_clock_skew() -> None:
    value = {"topic": "publicTrade.BTCUSDT", "ts": 3,
             "data": [{"s": "BTCUSDT", "T": 2}, {"s": "BTCUSDT", "T": 4}]}
    assert latency.raw_latency_samples(_market("bybit", "publicTrade.BTCUSDT", value, 1)) == (
        ("publicTrade.BTCUSDT", 2), ("publicTrade.BTCUSDT", 4))


def test_raw_latency_samples_exclude_non_timestamp_streams_and_empty_trades() -> None:
    active = {"channel": "activeAssetCtx", "data": {"coin": "BTC", "ctx": {}}}
    assert latency.raw_latency_samples(_market(
        "hyperliquid", "activeAssetCtx:BTC", active)) == ()
    assert latency.raw_latency_samples(_event("subscription_ack", 1)) == ()
    empty = {"channel": "trades", "data": []}
    assert latency.raw_latency_samples(_market("hyperliquid", "trades:BTC", empty)) == ()


@pytest.mark.parametrize("timestamp", [True, 0, -1])
def test_raw_latency_samples_reject_invalid_timestamps(timestamp: object) -> None:
    value = {"channel": "l2Book", "data": {"coin": "BTC", "time": timestamp}}
    with pytest.raises(ContractError):
        latency.raw_latency_samples(_market("hyperliquid", "l2Book:BTC", value))


def test_raw_latency_samples_reject_schema_drift_and_stream_mismatch() -> None:
    malformed = {"channel": "trades", "data": [
        {"coin": "BTC", "time": 1}, {"coin": "BTC", "time": "2"}]}
    wrong_stream = {"topic": "tickers.ETHUSDT", "ts": 1,
                    "data": {"symbol": "ETHUSDT"}}
    ts_only_book = {"topic": "orderbook.50.BTCUSDT", "ts": 1,
                    "data": {"s": "BTCUSDT"}}
    for event in (
        _market("hyperliquid", "trades:BTC", malformed),
        _market("bybit", "tickers.BTCUSDT", wrong_stream),
        _market("bybit", "orderbook.50.BTCUSDT", ts_only_book),
    ):
        with pytest.raises(ContractError):
            latency.raw_latency_samples(event)


def test_bybit_barrier_requires_both_snapshots_and_resets_per_connection() -> None:
    barrier = coverage.BybitBarrier()
    barrier.start("b1")
    assert not barrier.observe(_book("BTC", 1))
    assert not barrier.ready
    assert not barrier.observe(_book("ETH", 1))
    assert barrier.ready
    barrier.start("b2")
    assert not barrier.ready
    assert not barrier.observe(_book("ETH", 1))
    assert not barrier.ready


def test_bybit_barrier_freezes_delta_gaps_and_allows_snapshot_reset() -> None:
    barrier = coverage.BybitBarrier()
    barrier.start("missing")
    assert barrier.observe(_book("BTC", 1, "delta"))
    assert not barrier.ready
    barrier.start("continuous")
    assert not barrier.observe(_book("BTC", 1))
    assert not barrier.observe(_book("ETH", 1))
    assert not barrier.observe(_book("BTC", 2, "delta"))
    assert not barrier.observe(_book("BTC", 100))
    assert not barrier.observe(_book("BTC", 101, "delta"))
    assert barrier.ready
    assert barrier.observe(_book("BTC", 103, "delta"))
    assert not barrier.ready


def test_live_collector_delegates_sequence_judgment_to_shared_barrier() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(collector._Sink._sequence)))
    assert any(
        isinstance(node, ast.Attribute) and node.attr == "observe" for node in ast.walk(tree)
    )
    source = inspect.getsource(collector._Sink)
    assert "previous_u" not in source
    assert "sequence_topics" not in source
    assert not hasattr(collector, "bybit_update_gap")


def test_replay_emits_hard_points_and_requires_new_connection_after_failure(tmp_path: Path) -> None:
    events = [_config(), _event("subscription_send", 1),
              _event("subscription_ack", 2, ready=True),
              _event("application_heartbeat", 3, phase="pong"),
              _event("application_heartbeat", 4, phase="pong"),
              *_barrier("b1", 5, ready=False), *_barrier("b2", 10, ("ETH",)),
              _snapshot(15, "BTC", conn="b2", schema="raw_frame"),
              _event("raw_quarantine", 16, venue="bybit", conn="b2", raw="eA=="),
              _event("application_heartbeat", 17, venue="bybit", conn="b2", phase="pong"),
              *_barrier("b3", 18),
              _event("application_heartbeat", 23, venue="bybit", conn="b3", phase="pong")]
    _write(tmp_path, *events)
    point = coverage.CoveragePoint
    assert coverage.replay_coverage_points(tmp_path) == (
        point("hyperliquid", 3, "hard_verified", None),
        point("hyperliquid", 4, "hard_verified", None),
        point("bybit", 15, "hard_verified", None), point("bybit", 16, "unexplained_failure", None),
        point("bybit", 22, "hard_verified", None), point("bybit", 23, "hard_verified", None))


def test_failure_reasons_map_and_sequence_gap_must_match_raw(tmp_path: Path) -> None:
    reasons = ["application_pong_timeout", "subscription_ack_timeout",
               "transport_disconnected", "transport_ping_timeout"]
    events = [_config(), *[_event("liveness_failure", at, reason=reason)
                           for at, reason in enumerate(reasons, 1)],
              _event("venue_down", 5, conn="collector"), *_barrier("b1", 6),
              _snapshot(11, "BTC", conn="b1", update=3, kind="delta", schema="raw_frame"),
              _event("bybit_sequence_gap", 11, venue="bybit", conn="b1")]
    _write(tmp_path, *events)
    points = coverage.replay_coverage_points(tmp_path)
    assert [point.reason for point in points if point.kind == "explained_failure"] == [
        *reasons, "venue_down", "bybit_sequence_gap"]
    for label, suffix in (("missing", events[1:-1]), ("orphan", [*_barrier("b1", 6), events[-1]])):
        root = tmp_path / label
        _write(root, _config(), *suffix)
        with pytest.raises(ContractError):
            coverage.replay_coverage_points(root)


def test_formal_root_structure_order_and_integrity_are_fail_closed(tmp_path: Path) -> None:
    trial = _event("subscription_send", 1) | {"is_gate1_record": False}
    ledger = _event("account_ledger_entry", 1)
    ledger.update(event_kind="reconciliation", seq_within_boot=1, payload={
        "venue": "hyperliquid", "entry_id": "e", "entry_kind": "funding", "occurred_ns": 0,
        "asset": "USDC", "signed_amount_canonical": "1", "caused_by_order_id": None,
        "source_observed_ns": 0})
    cases = {"missing-config": [_event("subscription_send", 1)],
             "duplicate-config": [_config(), _config(1)], "trial": [_config(), trial],
             "unrelated": [_config(), ledger], "json": [_config(), b"{"],
             "backwards": [_config(2), _event("subscription_send", 1)]}
    for label, events in cases.items():
        root = tmp_path / label
        _write(root, *events)
        with pytest.raises(ContractError):
            coverage.replay_coverage_points(root)
    root = tmp_path / "checksum"
    _write(root, _config())
    shard = next((root / "shards").glob("*.raw.gz"))
    shard.write_bytes(shard.read_bytes() + b"x")
    with pytest.raises(ContractError):
        coverage.replay_coverage_points(root)
    assert "tuple(_formal_events(root))" in inspect.getsource(coverage.replay_coverage_points)
