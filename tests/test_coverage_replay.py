import ast
import inspect
import json
import textwrap

from data import collector, coverage


def _book(symbol: str, update: int, kind: str = "snapshot") -> bytes:
    return json.dumps({
        "topic": f"orderbook.50.{symbol}USDT", "type": kind, "data": {"u": update},
    }).encode()


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
    assert any(isinstance(node, ast.Attribute) and node.attr == "observe" for node in ast.walk(tree))
    source = inspect.getsource(collector._Sink)
    assert "previous_u" not in source
    assert "sequence_topics" not in source
    assert not hasattr(collector, "bybit_update_gap")
