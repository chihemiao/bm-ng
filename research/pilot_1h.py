import asyncio
import gzip
import json
import math
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from data.contracts import validate_envelope

HL_URL = "wss://api.hyperliquid.xyz/ws"
BYBIT_URL = "wss://stream.bybit.com/v5/public/linear"
COINS = ("BTC", "ETH")


def summarize_venue(**sample: Any) -> dict[str, Any]:
    duration_s = sample["duration_s"]
    message_count, raw_bytes = sample["message_count"], sample["raw_bytes"]
    compressed_bytes = sample["compressed_bytes"]
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    return {
        "venue": sample["venue"],
        "duration_s": duration_s,
        "messages": message_count,
        "messages_per_second": message_count / duration_s,
        "raw_bytes": raw_bytes,
        "compressed_bytes": compressed_bytes,
        "compression_ratio": compressed_bytes / raw_bytes if raw_bytes else None,
        "disk_bytes_per_hour": compressed_bytes / duration_s * 3_600,
        "reconnects": sample["reconnects"],
        "interarrival_ms": _arrival_stats(sample["intervals_ms"]),
        "raw_quarantine": sample["raw_quarantine"],
    }


def _arrival_stats(intervals_ms: list[float]) -> dict[str, float | None]:
    ordered = sorted(intervals_ms)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "p50": statistics.median(ordered) if ordered else None,
        "p95": ordered[p95_index] if ordered else None,
        "max": ordered[-1] if ordered else None,
    }


def summarize_stream(*, venue, channel, symbol, intervals_ms):
    return {
        "venue": venue,
        "channel": channel,
        "symbol": symbol,
        "messages": len(intervals_ms) + 1,
        "interarrival_ms": _arrival_stats(intervals_ms),
    }


def _stream(decoded: dict[str, Any]) -> tuple[str, str] | None:
    if topic := decoded.get("topic"):
        channel, symbol = topic.rsplit(".", 1)
        return channel, symbol.removesuffix("USDT")
    data = decoded.get("data")
    if isinstance(data, list):
        data = data[0] if data else {}
    if decoded.get("channel") and isinstance(data, dict) and data.get("coin"):
        return decoded["channel"], data["coin"]
    return None


def _event(venue, raw, conn_id, boot_id):
    quarantined = False
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = None
        quarantined = True
    control = (
        isinstance(decoded, dict)
        and not decoded.get("topic")
        and (decoded.get("channel") in {"subscriptionResponse", "pong"} or "op" in decoded)
    )
    payload_schema = "raw_frame"
    if control:
        payload_schema = "control_frame"
    if quarantined:
        payload_schema = "raw_quarantine"
    event = {
        "schema_ver": 1,
        "event_kind": "ops" if quarantined or control else "market",
        "payload_schema": payload_schema,
        "venue": venue,
        "conn_id": conn_id,
        "boot_id": boot_id,
        "recv_wall_ns": time.time_ns(),
        "recv_mono_ns": time.monotonic_ns(),
        "source": "pilot_public_ws",
        "payload": {"raw": raw},
    }
    stream = _stream(decoded) if isinstance(decoded, dict) and not control else None
    return validate_envelope(event), quarantined, stream


async def _collect(venue, url, subscriptions, ping, duration_s, output, boot_id):
    started = asyncio.get_running_loop().time()
    deadline = started + duration_s
    arrivals: dict[tuple[str, str], list[float]] = defaultdict(list)
    previous: dict[tuple[str, str], int] = {}
    message_count = raw_bytes = quarantined = 0
    conn_id = f"{venue}-{time.time_ns()}"
    next_ping = started + 20
    with gzip.open(output, "wb", compresslevel=6) as sink:
        async with connect(url, ping_interval=None, open_timeout=20, close_timeout=5) as websocket:
            for subscription in subscriptions:
                await websocket.send(json.dumps(subscription, separators=(",", ":")))
            while (now := asyncio.get_running_loop().time()) < deadline:
                timeout = min(deadline - now, max(0.001, next_ping - now))
                try:
                    frame = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                except TimeoutError:
                    await websocket.send(json.dumps(ping, separators=(",", ":")))
                    next_ping += 20
                    continue
                raw = frame.decode() if isinstance(frame, bytes) else frame
                event, is_quarantined, stream = _event(venue, raw, conn_id, boot_id)
                encoded = (json.dumps(event, separators=(",", ":")) + "\n").encode()
                sink.write(encoded)
                current_ns = event["recv_mono_ns"]
                for key in (("", ""), stream):
                    if key is not None:
                        if key in previous:
                            arrivals[key].append((current_ns - previous[key]) / 1_000_000)
                        previous[key] = current_ns
                message_count += 1
                raw_bytes += len(encoded)
                quarantined += is_quarantined
                if asyncio.get_running_loop().time() >= next_ping:
                    await websocket.send(json.dumps(ping, separators=(",", ":")))
                    next_ping += 20
            actual_duration = asyncio.get_running_loop().time() - started

    summary = summarize_venue(
        venue=venue,
        duration_s=actual_duration,
        message_count=message_count,
        raw_bytes=raw_bytes,
        compressed_bytes=output.stat().st_size,
        reconnects=0,
        intervals_ms=arrivals[("", "")],
        raw_quarantine=quarantined,
    )
    summary["streams"] = [
        summarize_stream(venue=venue, channel=channel, symbol=symbol, intervals_ms=values)
        for (channel, symbol), values in sorted(arrivals.items())
        if channel
    ]
    return summary


def _subscriptions() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    hl = [
        {"method": "subscribe", "subscription": {"type": feed, "coin": coin}}
        for coin in COINS
        for feed in ("l2Book", "trades", "bbo", "activeAssetCtx")
    ]
    bybit_topics = [
        f"{feed}.{coin}USDT"
        for coin in COINS
        for feed in ("orderbook.50", "publicTrade", "tickers")
    ]
    return hl, [{"op": "subscribe", "args": bybit_topics}]


async def run_pilot(duration_s: float) -> list[dict[str, Any]]:
    hl_subscriptions, bybit_subscriptions = _subscriptions()
    boot_id = f"pilot-{time.time_ns()}"
    with tempfile.TemporaryDirectory(prefix="hl-public-pilot-") as directory:
        root = Path(directory)
        jobs = (
            ("hyperliquid", HL_URL, hl_subscriptions, {"method": "ping"}, "hl.gz"),
            ("bybit", BYBIT_URL, bybit_subscriptions, {"op": "ping"}, "bybit.gz"),
        )
        return await asyncio.gather(
            *(
                _collect(venue, url, subs, ping, duration_s, root / name, boot_id)
                for venue, url, subs, ping, name in jobs
            )
        )


if __name__ == "__main__":
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 3_600
    print(json.dumps(asyncio.run(run_pilot(duration)), indent=2, sort_keys=True))
