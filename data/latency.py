"""Replay-only normalization of venue exchange timestamps."""

import base64
import json

from data.contracts import ContractError
from data.schema_dispatch import BYBIT_WIRE_SYMBOLS

_HL_CHANNELS = frozenset({"l2Book", "trades", "bbo", "activeAssetCtx"})
_BYBIT_CHANNELS = frozenset({"orderbook.50", "publicTrade", "tickers"})
_HL_STREAMS = frozenset(
    f"{channel}:{coin}" for channel in _HL_CHANNELS for coin in BYBIT_WIRE_SYMBOLS
)
_BYBIT_STREAMS = frozenset(
    f"{channel}.{symbol}" for channel in _BYBIT_CHANNELS for symbol in BYBIT_WIRE_SYMBOLS.values()
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _wire_value(event: dict) -> dict:
    try:
        raw = base64.b64decode(event["payload"]["raw"], validate=True)
        value = json.loads(raw)
        _require(type(value) is dict, "market frame must be an object")
        return value
    except (KeyError, TypeError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ContractError("invalid raw market frame") from error


def _hl_times(stream: str, value: dict) -> tuple[int, ...]:
    channel, data = value.get("channel"), value.get("data")
    _require(
        stream in _HL_STREAMS and channel == stream.split(":", 1)[0], "Hyperliquid stream mismatch"
    )
    if channel == "trades":
        _require(type(data) is list, "invalid Hyperliquid trades")
        items = data
    else:
        _require(type(data) is dict, "invalid Hyperliquid market data")
        items = [data]
    coin = stream.split(":", 1)[1]
    _require(
        all(type(item) is dict and item.get("coin") == coin for item in items),
        "Hyperliquid coin mismatch",
    )
    if channel == "activeAssetCtx":
        return ()
    return tuple(item.get("time") for item in items)


def _bybit_times(stream: str, value: dict) -> tuple[int, ...]:
    topic, data = value.get("topic"), value.get("data")
    _require(stream in _BYBIT_STREAMS and topic == stream, "Bybit stream mismatch")
    channel, symbol = stream.rsplit(".", 1)
    if channel == "publicTrade":
        _require(type(data) is list, "invalid Bybit trades")
        _require(
            all(type(item) is dict and item.get("s") == symbol for item in data),
            "Bybit trade symbol mismatch",
        )
        return tuple(item.get("T") for item in data)
    _require(type(data) is dict, "invalid Bybit market data")
    symbol_field = "s" if channel == "orderbook.50" else "symbol"
    _require(data.get(symbol_field) == symbol, "Bybit symbol mismatch")
    return (value.get("cts") if channel == "orderbook.50" else value.get("ts"),)


def raw_latency_samples(event: dict) -> tuple[tuple[str, int], ...]:
    """Return every exchange-time sample without comparing venue and local clocks."""
    if type(event) is not dict or event.get("payload_schema") != "raw_frame":
        return ()
    try:
        venue, stream = event["venue"], event["payload"]["stream"]
        _require(type(stream) is str, "invalid durable stream")
        value = _wire_value(event)
        if venue == "hyperliquid":
            times = _hl_times(stream, value)
        else:
            _require(venue == "bybit", "invalid latency venue")
            times = _bybit_times(stream, value)
        _require(
            all(type(timestamp) is int and timestamp > 0 for timestamp in times),
            "invalid exchange timestamp",
        )
        return tuple((stream, timestamp) for timestamp in times)
    except (KeyError, TypeError, ValueError) as error:
        raise ContractError("invalid latency frame") from error
