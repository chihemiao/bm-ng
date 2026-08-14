import inspect
from dataclasses import FrozenInstanceError, fields, is_dataclass
from typing import get_type_hints

import pytest

import reconciliation.kill_switch as kill_switch
from data.collector import CollectorLivenessSnapshot

NAMES = ("orders", "positions", "naked_notional", "stablecoin", "data_liveness")


def _evidence(**changes):
    values = {name: True for name in NAMES} | changes
    if "data_liveness" not in inspect.signature(kill_switch.KnownEvidence).parameters:
        values.pop("data_liveness")
    return kill_switch.KnownEvidence(**values)


def _liveness(**changes):
    values = dict(
        file_integrity_ok=True,
        hl_last_verified_mono_ns=95,
        bybit_last_verified_mono_ns=95,
    )
    return CollectorLivenessSnapshot(**(values | changes))


@pytest.mark.parametrize(("changes", "expected"), [
    ({}, (True, False)),
    ({"hl_last_verified_mono_ns": 90, "bybit_last_verified_mono_ns": 90},
     (True, False)),
    ({"file_integrity_ok": False}, (False, True)),
    ({"hl_last_verified_mono_ns": None}, (False, True)),
    ({"bybit_last_verified_mono_ns": None}, (False, True)),
    ({"hl_last_verified_mono_ns": None, "bybit_last_verified_mono_ns": None},
     (False, True)),
    ({"hl_last_verified_mono_ns": 89}, (True, True)),
    ({"bybit_last_verified_mono_ns": 89}, (True, True)),
    ({"hl_last_verified_mono_ns": 101}, (True, True)),
])
def test_data_liveness_evidence_matrix(changes, expected) -> None:
    assert kill_switch.data_liveness_evidence(
        _liveness(**changes), now_ns=100, max_gap_ns=10) == expected


@pytest.mark.parametrize(("snapshot", "now_ns", "max_gap_ns", "error"), [
    (object(), 100, 10, TypeError),
    (_liveness(), True, 10, TypeError),
    (_liveness(), 0, 10, ValueError),
    (_liveness(), -1, 10, ValueError),
    (_liveness(), 100, True, TypeError),
    (_liveness(), 100, -1, ValueError),
])
def test_data_liveness_evidence_rejects_invalid_inputs(
    snapshot, now_ns, max_gap_ns, error,
) -> None:
    with pytest.raises(error):
        kill_switch.data_liveness_evidence(
            snapshot, now_ns=now_ns, max_gap_ns=max_gap_ns)


@pytest.mark.parametrize(("snapshot", "changes"), [
    (_liveness(file_integrity_ok=False), {"now_ns": True}),
    (_liveness(hl_last_verified_mono_ns=None), {"max_gap_ns": -1}),
])
def test_data_liveness_trigger_does_not_hide_invalid_other_inputs(
    snapshot, changes,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        kill_switch.data_liveness_evidence(
            snapshot, now_ns=changes.get("now_ns", 100),
            max_gap_ns=changes.get("max_gap_ns", 10))


def test_data_liveness_evidence_has_the_frozen_pure_contract() -> None:
    function = kill_switch.data_liveness_evidence
    assert tuple(inspect.signature(function).parameters) == (
        "snapshot", "now_ns", "max_gap_ns")
    assert get_type_hints(function) == {
        "snapshot": CollectorLivenessSnapshot,
        "now_ns": int,
        "max_gap_ns": int,
        "return": tuple[bool, bool],
    }


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
    if "data_liveness" not in inspect.signature(kill_switch.KnownEvidence).parameters:
        values.pop("data_liveness", None)
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


def test_unknown_data_liveness_routes_to_cancel_only() -> None:
    assert kill_switch.decide_kill_switch(
        triggered=False,
        known_evidence=_evidence(data_liveness=False),
        reconciliation_consistency=True,
        reconciliation_streak_triggered=False,
    ) == kill_switch.KillSwitchDecision("cancel_only_freeze")
