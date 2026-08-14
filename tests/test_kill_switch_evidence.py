import inspect
from dataclasses import FrozenInstanceError, fields, is_dataclass
from typing import get_type_hints

import pytest

import reconciliation.kill_switch as kill_switch

NAMES = ("orders", "positions", "naked_notional", "stablecoin")


def _evidence(**changes):
    values = {name: True for name in NAMES} | changes
    return kill_switch.KnownEvidence(**values)


def test_known_evidence_is_a_closed_keyword_only_value_object() -> None:
    evidence_type = getattr(kill_switch, "KnownEvidence", None)
    assert is_dataclass(evidence_type)
    assert [field.name for field in fields(evidence_type)] == list(NAMES)
    assert get_type_hints(evidence_type) == {name: bool for name in NAMES}
    assert evidence_type.__dataclass_params__.frozen and evidence_type.__slots__
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in inspect.signature(evidence_type).parameters.values()
    )
    evidence = _evidence()
    with pytest.raises(FrozenInstanceError):
        evidence.orders = False


@pytest.mark.parametrize("field", NAMES)
@pytest.mark.parametrize("value", [1, 0, None, "true"])
def test_known_evidence_requires_exact_booleans(field, value) -> None:
    with pytest.raises(TypeError, match=field):
        _evidence(**{field: value})


@pytest.mark.parametrize("missing", NAMES)
def test_known_evidence_requires_every_frozen_field(missing) -> None:
    values = {name: True for name in NAMES}
    del values[missing]
    with pytest.raises(TypeError, match=missing):
        kill_switch.KnownEvidence(**values)


@pytest.mark.parametrize("value", [None, {}, object()])
def test_decision_rejects_every_non_known_evidence_value(value) -> None:
    with pytest.raises(TypeError, match="known_evidence must be a KnownEvidence"):
        kill_switch.decide_kill_switch(
            triggered=False, known_evidence=value,
            reconciliation_consistency=True,
            reconciliation_streak_triggered=False,
        )


def test_decision_requires_the_single_named_evidence_value() -> None:
    signature = inspect.signature(kill_switch.decide_kill_switch)
    assert tuple(signature.parameters) == (
        "triggered", "known_evidence", "reconciliation_consistency",
        "reconciliation_streak_triggered",
    )
    assert get_type_hints(kill_switch.decide_kill_switch) == {
        "triggered": bool,
        "known_evidence": kill_switch.KnownEvidence,
        "reconciliation_consistency": bool | None,
        "reconciliation_streak_triggered": bool,
        "return": kill_switch.KillSwitchDecision,
    }
