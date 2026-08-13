from decimal import Decimal

import pytest

from execution.orders import make_t0a_pair_intents
from reconciliation.bybit_surface import BybitFilledQuantity
from reconciliation.legs import PairState, build_fill_pair_state
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


def test_missing_hl_fill_assembly_is_unknown_while_bybit_can_complete():
    assert build_fill_pair_state(
        PAIR, None, BYBIT_FILLED, now_ns=110, max_age_ns=10
    ) == PairState("unknown", (("hyperliquid", "unknown"),))


def test_missing_bybit_fill_assembly_remains_a_contract_error():
    with pytest.raises(TypeError, match="bybit_result"):
        build_fill_pair_state(PAIR, None, None, now_ns=110, max_age_ns=10)
