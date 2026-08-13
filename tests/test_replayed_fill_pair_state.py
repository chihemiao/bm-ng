import ast
import inspect
import textwrap
from dataclasses import replace
from decimal import Decimal

import pytest

from data.replay_order import OrderBinding
from data.shard import EventReplay
from execution.order_serde import serialize_order_request
from execution.orders import make_t0a_pair_intents, order_request_record
from reconciliation.bybit_surface import BybitFilledQuantity
from reconciliation.legs import (
    PairState,
    build_fill_pair_state,
    build_replayed_fill_pair_state,
)
from reconciliation.state import CanonicalSet, SurfaceEvidence

PAIR = make_t0a_pair_intents(
    "funding-carry", "git-deadbeef", 100, symbol="BTC", quantity=Decimal("1")
)
EVIDENCE = SurfaceEvidence(
    100, 1, True, False, 0, 0,
    CanonicalSet("fills.state", 1, frozenset({"state"})),
    CanonicalSet("fills.identity", 1, frozenset({"identity"})),
)
BYBIT_FILLED = BybitFilledQuantity(
    client_order_id=PAIR.bybit.client_order_id,
    quantity=Decimal("1"),
    evidence=EVIDENCE,
)
HL_FILL = {
    "closedPnl": "0", "coin": "BTC", "crossed": False, "dir": "Open Short",
    "fee": "0", "feeToken": "USDC", "hash": "0xabc", "oid": 7, "px": "1",
    "side": "A", "startPosition": "0", "sz": "1", "tid": 8, "time": 1000,
}
BYBIT_FILL = {
    "symbol": "BTCUSDT", "orderLinkId": PAIR.bybit.client_order_id, "side": "Buy",
    "execId": "execution-1", "execQty": "1", "execType": "Trade", "execTime": "1000",
}


def _request_event(intent, seq):
    request = order_request_record(
        intent, 110, account_digest="a" * 64, lease_epoch=1,
        writer_instance_id="writer-one", wallet_fingerprint="b" * 64,
        allocated_nonce=7 if intent.leg == "hyperliquid" else None,
    )
    return serialize_order_request(
        intent, request, conn_id="local", boot_id="boot-one", recv_wall_ns=110 + seq,
        recv_mono_ns=110 + seq, source="execution", seq_within_boot=seq,
    )


def _replay(*intents, frozen=False):
    bindings = ()
    if PAIR.hyperliquid in intents:
        bindings = (OrderBinding(
            venue="hyperliquid", client_order_id=PAIR.hyperliquid.client_order_id,
            venue_order_id="7",
        ),)
    return EventReplay(
        events=tuple(_request_event(intent, index) for index, intent in enumerate(intents, 1)),
        duplicate_digests=(), order_bindings=bindings,
        freeze_reasons=("controlled-freeze",) if frozen else (), input_digest="digest",
    )


def _bybit_pages():
    return ({
        "retCode": 0, "retMsg": "OK",
        "result": {"category": "linear", "nextPageCursor": "", "list": [BYBIT_FILL]},
        "retExtInfo": {}, "time": 1000,
    },)


def _assemble(replay, **changes):
    values = {
        "pair": PAIR, "replay": replay, "hyperliquid_pages": ([HL_FILL],),
        "bybit_pages": _bybit_pages(), "since_ms": 1000, "skew_allowance_ms": 0,
        "observed_ns": 100, "page_complete": True, "truncated": False,
        "now_ns": 110, "max_age_ns": 10,
    }
    values.update(changes)
    return build_replayed_fill_pair_state(**values)


def test_missing_hl_fill_assembly_is_unknown_while_bybit_can_complete():
    assert build_fill_pair_state(
        PAIR, None, BYBIT_FILLED, now_ns=110, max_age_ns=10
    ) == PairState("unknown", (("hyperliquid", "unknown"),))


def test_missing_bybit_fill_assembly_remains_a_contract_error():
    with pytest.raises(TypeError, match="bybit_result"):
        build_fill_pair_state(PAIR, None, None, now_ns=110, max_age_ns=10)


def test_replayed_requests_and_venue_fills_compose_to_balanced():
    assert _assemble(_replay(PAIR.hyperliquid, PAIR.bybit)) == PairState("balanced", ())


def test_replay_freeze_passes_request_gate_and_makes_hl_unknown():
    replay = _replay(PAIR.hyperliquid, PAIR.bybit, frozen=True)
    assert _assemble(replay) == PairState("unknown", (("hyperliquid", "unknown"),))


@pytest.mark.parametrize("intents", [(PAIR.hyperliquid,), (PAIR.bybit,)])
def test_each_missing_durable_pair_request_fails_before_fill_builders(intents):
    with pytest.raises(ValueError, match="durable requests"):
        _assemble(
            _replay(*intents), hyperliquid_pages=object(), bybit_pages=object()
        )


@pytest.mark.parametrize(
    ("pair", "replay", "error", "match"),
    [
        (object(), object(), TypeError, "pair"),
        (replace(PAIR, hyperliquid=replace(PAIR.hyperliquid, side="buy")), object(),
         ValueError, "pair intents"),
        (PAIR, object(), TypeError, "replay"),
    ],
)
def test_pair_and_replay_validation_precede_request_and_fill_parsing(
    pair, replay, error, match,
):
    with pytest.raises(error, match=match):
        _assemble(replay, pair=pair, hyperliquid_pages=object(), bybit_pages=object())


def test_replayed_pair_assembly_delegates_each_existing_stage():
    source = textwrap.dedent(inspect.getsource(build_replayed_fill_pair_state))
    calls = {
        node.func.id for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {
        "rehydrate_order_request", "build_replayed_hl_filled_quantity",
        "build_intent_bybit_filled_quantity", "build_fill_pair_state",
    } <= calls
