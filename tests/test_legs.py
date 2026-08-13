from dataclasses import replace
from decimal import Decimal

import pytest

import reconciliation.clock as clock_module
import reconciliation.exposure as exposure_module
import reconciliation.legs as legs_module
from execution.orders import make_t0a_pair_intents
from reconciliation.bybit_surface import BybitFilledQuantity
from reconciliation.clock import StateClock
from reconciliation.hl_fills import HLFilledQuantity
from reconciliation.legs import (
    LegOutcome,
    PairState,
    advance_obligation_clock,
    build_fill_pair_state,
    leg_completion,
    obligation_state,
    pair_state,
)
from reconciliation.state import CanonicalSet, SurfaceEvidence, surface_is_authoritative


def _canonical(kind: str) -> CanonicalSet:
    return CanonicalSet(f"fills.{kind}", 1, frozenset({kind}))


def _evidence(**changes) -> SurfaceEvidence:
    values = {
        "observed_ns": 100,
        "fetched_count": 1,
        "page_complete": True,
        "truncated": False,
        "unknown_count": 0,
        "mismatch_count": 0,
        "entities": _canonical("state"),
        "identities": _canonical("identity"),
    }
    values.update(changes)
    return SurfaceEvidence(**values)


def _completion(filled, **changes) -> str:
    values = {
        "intended_quantity": Decimal("1"),
        "filled_quantity": None if filled is None else Decimal(filled),
        "evidence": _evidence(),
        "now_ns": 110,
        "max_age_ns": 10,
    }
    values.update(changes)
    return leg_completion(**values)


@pytest.mark.parametrize(
    ("filled", "expected"),
    [("0", "none"), ("0.4", "partial"), ("1", "complete"), ("1.1", "overfilled")],
)
def test_authoritative_fill_quantity_has_a_closed_completion_state(filled, expected):
    assert _completion(filled) == expected


def test_missing_filled_quantity_is_unknown_not_none_filled():
    assert _completion(None) == "unknown"


@pytest.mark.parametrize(
    ("changes", "now_ns", "max_age_ns"),
    [
        ({"page_complete": False}, 110, 10),
        ({"truncated": True}, 110, 10),
        ({"unknown_count": 1, "fetched_count": 2}, 110, 10),
        ({"mismatch_count": 1}, 110, 10),
        ({"observed_ns": 99}, 110, 10),
        ({"observed_ns": 111}, 110, 10),
    ],
)
def test_non_authoritative_or_nonfresh_fill_surface_is_unknown(changes, now_ns, max_age_ns):
    assert (
        _completion("1", evidence=_evidence(**changes), now_ns=now_ns, max_age_ns=max_age_ns)
        == "unknown"
    )


def test_non_authoritative_exact_fill_is_unknown_before_arithmetic():
    assert _completion("1", evidence=_evidence(truncated=True)) == "unknown"


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["intended_quantity", "filled_quantity"])
def test_non_finite_quantities_are_value_errors(field, value):
    with pytest.raises(ValueError) as raised:
        _completion("0", **{field: Decimal(value)})
    assert type(raised.value) is ValueError


@pytest.mark.parametrize(
    "changes",
    [
        {"intended_quantity": Decimal("0")},
        {"intended_quantity": Decimal("-1")},
        {"filled_quantity": Decimal("-0.1")},
    ],
)
def test_quantity_domains_are_enforced(changes):
    with pytest.raises(ValueError):
        _completion("0", **changes)


@pytest.mark.parametrize(
    "changes",
    [{"intended_quantity": 1.0}, {"filled_quantity": 0.0}],
)
def test_quantities_reject_non_decimal_numbers(changes):
    with pytest.raises(TypeError):
        _completion("0", **changes)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"now_ns": True}, TypeError),
        ({"max_age_ns": 1.0}, TypeError),
        ({"now_ns": 0}, ValueError),
        ({"max_age_ns": 0}, ValueError),
    ],
)
def test_completion_clock_inputs_are_strictly_positive_integers(changes, error):
    with pytest.raises(error):
        _completion("0", **changes)


def test_exposure_and_legs_share_the_same_authoritativeness_predicate():
    assert exposure_module.surface_is_authoritative is surface_is_authoritative
    assert legs_module.surface_is_authoritative is surface_is_authoritative


def _pair(hyperliquid: str, bybit: str) -> list[LegOutcome]:
    return [LegOutcome("hyperliquid", hyperliquid), LegOutcome("bybit", bybit)]


def test_two_complete_legs_are_the_only_fully_resolved_pair():
    assert pair_state(_pair("complete", "complete")) == PairState("balanced", ())


def test_two_unfilled_legs_are_not_misreported_as_a_completed_balance():
    assert pair_state(_pair("none", "none")) == PairState(
        "unfilled", (("bybit", "none"), ("hyperliquid", "none"))
    )


@pytest.mark.parametrize(
    ("hyperliquid", "bybit", "unresolved"),
    [
        ("complete", "none", (("bybit", "none"),)),
        ("complete", "partial", (("bybit", "partial"),)),
        (
            "partial",
            "partial",
            (("bybit", "partial"), ("hyperliquid", "partial")),
        ),
    ],
)
def test_underfilled_pair_shapes_share_one_imbalanced_label(hyperliquid, bybit, unresolved):
    assert pair_state(_pair(hyperliquid, bybit)) == PairState("imbalanced", unresolved)


@pytest.mark.parametrize(
    ("hyperliquid", "bybit", "unresolved"),
    [
        ("overfilled", "complete", (("hyperliquid", "overfilled"),)),
        (
            "overfilled",
            "partial",
            (("bybit", "partial"), ("hyperliquid", "overfilled")),
        ),
    ],
)
def test_known_overfill_has_a_distinct_label_and_remains_unresolved(hyperliquid, bybit, unresolved):
    assert pair_state(_pair(hyperliquid, bybit)) == PairState("overfilled", unresolved)


@pytest.mark.parametrize(
    ("hyperliquid", "bybit", "unresolved"),
    [
        ("unknown", "complete", (("hyperliquid", "unknown"),)),
        (
            "unknown",
            "overfilled",
            (("bybit", "overfilled"), ("hyperliquid", "unknown")),
        ),
        (
            "unknown",
            "unknown",
            (("bybit", "unknown"), ("hyperliquid", "unknown")),
        ),
    ],
)
def test_unknown_label_dominates_without_hiding_known_unresolved_information(
    hyperliquid, bybit, unresolved
):
    assert pair_state(_pair(hyperliquid, bybit)) == PairState("unknown", unresolved)


@pytest.mark.parametrize(
    "legs",
    [
        [LegOutcome("hyperliquid", "complete")],
        [LegOutcome("hyperliquid", "complete"), LegOutcome("hyperliquid", "none")],
        [LegOutcome("hyperliquid", "complete"), LegOutcome("other", "none")],
    ],
)
def test_pair_requires_each_frozen_venue_exactly_once(legs):
    with pytest.raises(ValueError, match="venue"):
        pair_state(legs)


def test_invalid_completion_is_rejected_not_skipped():
    with pytest.raises(ValueError, match="completion"):
        pair_state(_pair("complete", "other"))


@pytest.mark.parametrize(
    "legs",
    [
        [object(), LegOutcome("bybit", "complete")],
        [LegOutcome(1, "complete"), LegOutcome("bybit", "complete")],
        [LegOutcome("hyperliquid", 1), LegOutcome("bybit", "complete")],
    ],
)
def test_pair_rejects_non_outcome_or_non_string_fields(legs):
    with pytest.raises(TypeError):
        pair_state(legs)


def test_unresolved_order_is_deterministic_regardless_of_input_order():
    forward = _pair("unknown", "overfilled")
    assert pair_state(forward) == pair_state(list(reversed(forward)))


def test_fully_resolved_pair_is_the_only_settled_obligation_state():
    assert obligation_state(pair_state(_pair("complete", "complete"))) == "settled"


@pytest.mark.parametrize(
    ("hyperliquid", "bybit"),
    [
        ("none", "none"),
        ("complete", "partial"),
        ("overfilled", "complete"),
        ("unknown", "complete"),
    ],
)
def test_every_unresolved_pair_state_is_an_outstanding_obligation(hyperliquid, bybit):
    assert obligation_state(pair_state(_pair(hyperliquid, bybit))) == "outstanding"


def test_first_outstanding_observation_starts_the_pair_level_clock():
    pair = pair_state(_pair("unknown", "complete"))
    assert advance_obligation_clock(
        None, pair=pair, observed_ns=100, max_outstanding_ns=0
    ) == StateClock("active", 100, 100, False)


@pytest.mark.parametrize(("observed_ns", "exceeded"), [(110, False), (111, True)])
def test_outstanding_duration_exceeds_only_after_the_inclusive_limit(observed_ns, exceeded):
    previous = StateClock("active", 100, 100, False)
    pair = pair_state(_pair("complete", "partial"))
    result = advance_obligation_clock(
        previous, pair=pair, observed_ns=observed_ns, max_outstanding_ns=10
    )
    assert result.duration_exceeded is exceeded


def test_settled_pair_clears_even_an_exceeded_obligation_clock():
    previous = StateClock("active", 111, 100, True)
    pair = pair_state(_pair("complete", "complete"))
    assert advance_obligation_clock(
        previous, pair=pair, observed_ns=112, max_outstanding_ns=10
    ) == StateClock("inactive", 112, None, False)


def test_obligation_clock_rejects_backward_observation_time():
    previous = StateClock("active", 100, 100, False)
    pair = pair_state(_pair("complete", "partial"))
    with pytest.raises(ValueError, match="observed_ns"):
        advance_obligation_clock(previous, pair=pair, observed_ns=99, max_outstanding_ns=10)


def test_obligation_clock_rejects_different_state_at_the_same_time():
    previous = StateClock("active", 100, 100, False)
    settled = pair_state(_pair("complete", "complete"))
    with pytest.raises(ValueError, match="same observed_ns"):
        advance_obligation_clock(previous, pair=settled, observed_ns=100, max_outstanding_ns=10)


def test_obligation_clock_rejects_untyped_pair_input():
    with pytest.raises(TypeError, match="PairState"):
        advance_obligation_clock(None, pair="outstanding", observed_ns=100, max_outstanding_ns=10)


def test_obligation_wrapper_uses_the_shared_state_clock_function():
    assert legs_module.advance_state_clock is clock_module.advance_state_clock


INTENT_PAIR = make_t0a_pair_intents(
    "funding-carry", "git-deadbeef", 100, symbol="BTC", quantity=Decimal("1")
)
HL_RESULT = HLFilledQuantity(client_order_id=INTENT_PAIR.hyperliquid.client_order_id,
                             quantity=Decimal("1"), evidence=_evidence())
BYBIT_RESULT = BybitFilledQuantity(client_order_id=INTENT_PAIR.bybit.client_order_id,
                                   quantity=Decimal("1"), evidence=_evidence())


def _build_pair(**changes) -> PairState:
    values = {"pair": INTENT_PAIR, "hl_result": HL_RESULT, "bybit_result": BYBIT_RESULT,
              "now_ns": 110, "max_age_ns": 10}
    values.update(changes)
    return build_fill_pair_state(**values)


def test_real_t0a_intents_and_fill_results_compose_to_balanced():
    assert _build_pair() == PairState("balanced", ())


@pytest.mark.parametrize(
    ("quantity_field", "venue"),
    [("hl_quantity", "hyperliquid"), ("bybit_quantity", "bybit")],
)
def test_each_venue_unknown_quantity_symmetrically_dominates_pair(quantity_field, venue):
    result_field = quantity_field.removesuffix("_quantity") + "_result"
    result = replace(
        {"hl_result": HL_RESULT, "bybit_result": BYBIT_RESULT}[result_field], quantity=None
    )
    assert _build_pair(**{result_field: result}) == PairState("unknown", ((venue, "unknown"),))


def test_two_unknown_quantities_remain_visible_in_pair_state():
    unresolved = (("bybit", "unknown"), ("hyperliquid", "unknown"))
    assert _build_pair(
        hl_result=replace(HL_RESULT, quantity=None),
        bybit_result=replace(BYBIT_RESULT, quantity=None),
    ) == PairState("unknown", unresolved)


@pytest.mark.parametrize(
    ("evidence_field", "venue"),
    [("hl_evidence", "hyperliquid"), ("bybit_evidence", "bybit")],
)
def test_each_fill_result_keeps_its_own_authoritativeness(evidence_field, venue):
    result_field = evidence_field.removesuffix("_evidence") + "_result"
    result = replace(
        {"hl_result": HL_RESULT, "bybit_result": BYBIT_RESULT}[result_field],
        evidence=_evidence(truncated=True),
    )
    assert _build_pair(**{result_field: result}) == PairState("unknown", ((venue, "unknown"),))


def test_pair_type_is_checked_before_result_types():
    with pytest.raises(TypeError, match="T0APairIntents"):
        _build_pair(pair=None, hl_result=None, bybit_result=None)


@pytest.mark.parametrize("field", ["hl_result", "bybit_result"])
def test_fill_result_types_are_exactly_venue_bound(field):
    with pytest.raises(TypeError, match=field):
        _build_pair(**{field: object()})


def test_mismatched_t0a_pair_is_rejected_before_composition():
    invalid = replace(INTENT_PAIR, hyperliquid=replace(INTENT_PAIR.hyperliquid, side="buy"))
    with pytest.raises(ValueError, match="pair intents"):
        _build_pair(pair=invalid)


@pytest.mark.parametrize(
    ("field", "client_order_id"), [("hl_result", "wrong-hl"), ("bybit_result", "wrong-bybit")]
)
def test_each_fill_result_must_match_its_venue_intent(field, client_order_id):
    result = replace(
        {"hl_result": HL_RESULT, "bybit_result": BYBIT_RESULT}[field],
        client_order_id=client_order_id,
    )
    with pytest.raises(ValueError, match=field):
        _build_pair(**{field: result})


@pytest.mark.parametrize(
    ("changes", "error"),
    [({"now_ns": True}, TypeError), ({"max_age_ns": 0}, ValueError)],
)
def test_pair_composer_forwards_strict_clock_contract(changes, error):
    with pytest.raises(error):
        _build_pair(**changes)
