import ast
import inspect
import textwrap
from dataclasses import replace
from decimal import Decimal

import pytest

from data.replay_order import OrderBinding
from data.shard import EventReplay
from execution.orders import make_order_intent
from reconciliation import hl_fills

FILL = {
    "closedPnl": "0.0", "coin": "BTC", "crossed": False, "dir": "Open Long",
    "hash": "0xabc", "oid": 90542681, "px": "18.435", "side": "B",
    "startPosition": "0", "sz": "1", "time": 1681222254710, "fee": "0.01",
    "feeToken": "USDC", "tid": 118906512037719,
}
INTENT = make_order_intent(
    "funding-carry", "git-deadbeef", 100, "hyperliquid",
    symbol="BTC", side="buy", quantity=Decimal("1"),
)
BINDING = OrderBinding(
    venue="hyperliquid",
    client_order_id=INTENT.client_order_id,
    venue_order_id=str(FILL["oid"]),
)
REPLAY = EventReplay((), (), (BINDING,), (), "replay-digest")


def _build(replay=REPLAY, pages=None, *, intent=INTENT):
    return hl_fills.build_replayed_hl_filled_quantity(
        replay,
        [[FILL]] if pages is None else pages,
        intent=intent,
        since_ms=FILL["time"],
        skew_allowance_ms=0,
        observed_ns=200,
        page_complete=True,
        truncated=False,
    )


def test_replay_binding_is_the_only_order_id_source_for_fill_aggregation():
    expected = hl_fills.build_hl_filled_quantity(
        [[FILL]], coin="BTC", intended_side="buy",
        oids=frozenset({FILL["oid"]}), client_order_id=INTENT.client_order_id,
        since_ms=FILL["time"],
        skew_allowance_ms=0, observed_ns=200,
        page_complete=True, truncated=False,
    )
    assert _build() == expected
    assert _build().client_order_id == INTENT.client_order_id


def test_missing_binding_is_unknown_without_running_the_fill_aggregator():
    assert _build(replace(REPLAY, order_bindings=()), pages=object()) is None


def test_frozen_replay_keeps_all_ids_queryable_but_blocks_fill_aggregation():
    other = replace(BINDING, venue_order_id="8")
    frozen = replace(
        REPLAY,
        order_bindings=(BINDING, other),
        freeze_reasons=("order_observation:client_order_id_conflict",),
    )

    assert hl_fills.hl_order_ids(
        frozen.order_bindings, client_order_id=INTENT.client_order_id
    ) == frozenset({8, FILL["oid"]})
    assert _build(frozen, pages=object()) is None


def test_invalid_matching_id_is_rejected_before_the_replay_freeze_gate():
    frozen = replace(
        REPLAY,
        order_bindings=(replace(BINDING, venue_order_id="00"),),
        freeze_reasons=("replay:frozen",),
    )
    with pytest.raises(ValueError, match="venue_order_id"):
        _build(frozen, pages=object())


@pytest.mark.parametrize(
    "replay,intent,error,match",
    [
        (object(), INTENT, TypeError, "replay"),
        (REPLAY, object(), TypeError, "intent"),
        (
            REPLAY,
            make_order_intent(
                "funding-carry", "git-deadbeef", 100, "bybit",
                symbol="BTC", side="buy", quantity=Decimal("1"),
            ),
            ValueError,
            "hyperliquid",
        ),
    ],
)
def test_assembly_owns_replay_and_hyperliquid_intent_boundaries(
    replay, intent, error, match
):
    with pytest.raises(error, match=match):
        _build(replay, pages=object(), intent=intent)


def test_assembly_signature_calls_both_sources_and_documents_conditional_validation():
    function = hl_fills.build_replayed_hl_filled_quantity
    assert tuple(inspect.signature(function).parameters) == (
        "replay", "pages", "intent", "since_ms", "skew_allowance_ms",
        "observed_ns", "page_complete", "truncated",
    )
    source = textwrap.dedent(inspect.getsource(function))
    calls = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"hl_order_ids", "build_hl_filled_quantity"} <= calls
    assert "same intent.client_order_id" in inspect.getdoc(function)
    assert "only when aggregation runs" in inspect.getdoc(function)
