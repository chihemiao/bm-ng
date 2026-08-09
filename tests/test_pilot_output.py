import pytest

from research.pilot_1h import summarize_stream, summarize_venue


def test_pilot_summary_exposes_frozen_measurements() -> None:
    summary = summarize_venue(
        venue="hyperliquid",
        duration_s=2.0,
        message_count=4,
        raw_bytes=100,
        compressed_bytes=25,
        reconnects=1,
        intervals_ms=[10.0, 20.0, 30.0, 40.0],
        raw_quarantine=0,
    )

    assert summary == {
        "venue": "hyperliquid",
        "duration_s": 2.0,
        "messages": 4,
        "messages_per_second": 2.0,
        "raw_bytes": 100,
        "compressed_bytes": 25,
        "compression_ratio": 0.25,
        "disk_bytes_per_hour": 45_000.0,
        "reconnects": 1,
        "interarrival_ms": {"p50": 25.0, "p95": 40.0, "max": 40.0},
        "raw_quarantine": 0,
    }


@pytest.mark.parametrize("duration_s", [0.0, -1.0])
def test_pilot_summary_rejects_nonpositive_duration(duration_s: float) -> None:
    with pytest.raises(ValueError, match="duration_s"):
        summarize_venue(
            venue="bybit",
            duration_s=duration_s,
            message_count=0,
            raw_bytes=0,
            compressed_bytes=0,
            reconnects=0,
            intervals_ms=[],
            raw_quarantine=0,
        )


def test_stream_summary_uses_venue_channel_symbol_granularity() -> None:
    assert summarize_stream(
        venue="hyperliquid",
        channel="l2Book",
        symbol="BTC",
        intervals_ms=[10.0, 20.0, 30.0, 40.0],
    ) == {
        "venue": "hyperliquid",
        "channel": "l2Book",
        "symbol": "BTC",
        "messages": 5,
        "interarrival_ms": {"p50": 25.0, "p95": 40.0, "max": 40.0},
    }
