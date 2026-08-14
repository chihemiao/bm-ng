import ast
import inspect
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError, fields
from decimal import Decimal
from typing import get_args, get_type_hints

import pytest

import reconciliation.kill_switch as kill_switch
from data.contracts import ROTATION_LEAD_NS, VALIDITY_NS
from execution.wallet import AgentWalletRegistration
from reconciliation.exposure import ExposureClock
from reconciliation.fx import Notional
from reconciliation.kill_switch import (
    KILL_SWITCH_KEY_EXPIRY_LEAD_NS,
    KillSwitchDecision,
    decide_kill_switch,
    exposure_kill_trigger,
    key_and_nonce_triggered,
    key_expiry_triggered,
    nonce_anomaly_triggered,
)

DAY_NS = 86_400 * 1_000_000_000
ISSUED_NS = DAY_NS
REGISTRATION = AgentWalletRegistration(
    "a" * 64, ISSUED_NS, ISSUED_NS + VALIDITY_NS
)
WALLET = "b" * 64
OTHER_WALLET = "c" * 64


def _nonce_event(
    *, outcome="allocated", reason="nonce_allocated", allocated=11,
    previous=10, fingerprint=WALLET,
):
    return {
        "payload_schema": "signer_nonce_allocation",
        "payload": {
            "wallet_fingerprint": fingerprint,
            "outcome": outcome,
            "reason": reason,
            "allocated_nonce": allocated,
            "previous_nonce": previous,
        },
    }


def _freeze(reason="clock_backward", *, fingerprint=WALLET):
    return _nonce_event(
        outcome="frozen", reason=reason, allocated=None, fingerprint=fingerprint,
    )


@pytest.mark.parametrize(
    ("remaining_ns", "triggered"),
    [
        (7 * DAY_NS + 1, False),
        (7 * DAY_NS, False),
        (7 * DAY_NS - 1, True),
        (0, True),
        (-1, True),
    ],
)
def test_key_expiry_uses_the_strict_gate4_boundary(remaining_ns, triggered) -> None:
    now_ns = REGISTRATION.expires_ns - remaining_ns
    assert key_expiry_triggered(REGISTRATION, now_ns=now_ns) is triggered


@pytest.mark.parametrize(
    ("registration", "error"),
    [(object(), TypeError), (None, TypeError)],
)
def test_key_expiry_requires_a_wallet_registration(registration, error) -> None:
    with pytest.raises(error, match="registration"):
        key_expiry_triggered(registration, now_ns=ISSUED_NS)


@pytest.mark.parametrize(
    ("now_ns", "error"),
    [(True, TypeError), (1.0, TypeError), (None, TypeError), (0, ValueError), (-1, ValueError)],
)
def test_key_expiry_requires_a_positive_exact_integer_clock(now_ns, error) -> None:
    with pytest.raises(error, match="now_ns"):
        key_expiry_triggered(REGISTRATION, now_ns=now_ns)


def test_gate4_lead_time_is_independent_from_wallet_rotation_policy() -> None:
    assert KILL_SWITCH_KEY_EXPIRY_LEAD_NS == ROTATION_LEAD_NS == 7 * DAY_NS
    assert "ROTATION_LEAD_NS" not in inspect.getsource(
        inspect.getmodule(key_expiry_triggered)
    )


def test_key_expiry_detector_does_not_delegate_to_wallet_assessment() -> None:
    tree = ast.parse(inspect.getsource(key_expiry_triggered))
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "assess" not in called_names


@pytest.mark.parametrize(
    "events",
    [
        [_freeze()],
        [_freeze("fence_invalidated")],
        [_nonce_event(), _nonce_event(allocated=13, previous=12)],
        [_nonce_event(), _freeze(), _nonce_event(allocated=12, previous=11)],
    ],
)
def test_signer_nonce_freezes_and_chain_conflicts_trigger(events) -> None:
    assert nonce_anomaly_triggered(events, wallet_fingerprint=WALLET) is True


@pytest.mark.parametrize(
    "events",
    [[], [_nonce_event()], [_freeze(fingerprint=OTHER_WALLET)]],
)
def test_clean_or_other_signer_nonce_stream_does_not_trigger(events) -> None:
    assert nonce_anomaly_triggered(events, wallet_fingerprint=WALLET) is False


def test_generator_is_materialized_before_both_nonce_replays() -> None:
    events = (_nonce_event(**changes) for changes in (
        {}, {"allocated": 13, "previous": 12},
    ))
    assert nonce_anomaly_triggered(events, wallet_fingerprint=WALLET) is True


def test_duplicate_freeze_error_propagates() -> None:
    with pytest.raises(ValueError, match="multiple signer nonce freeze rows"):
        nonce_anomaly_triggered(
            [_freeze(), _freeze("fence_invalidated")], wallet_fingerprint=WALLET,
        )


def test_conflict_validation_runs_even_after_a_freeze_trigger() -> None:
    malformed = _nonce_event(allocated=True)
    with pytest.raises(ValueError, match="allocated_nonce"):
        nonce_anomaly_triggered([_freeze(), malformed], wallet_fingerprint=WALLET)


@pytest.mark.parametrize(
    ("wallet_fingerprint", "error"), [(None, TypeError), ("", ValueError)]
)
def test_nonce_anomaly_requires_a_valid_wallet_fingerprint(
    wallet_fingerprint, error,
) -> None:
    with pytest.raises(error, match="wallet_fingerprint"):
        nonce_anomaly_triggered([], wallet_fingerprint=wallet_fingerprint)


def test_nonce_detector_has_the_frozen_pure_sequence_contract() -> None:
    signature = inspect.signature(nonce_anomaly_triggered)
    assert tuple(signature.parameters) == ("events", "wallet_fingerprint")
    assert signature.parameters["wallet_fingerprint"].kind is inspect.Parameter.KEYWORD_ONLY
    hints = get_type_hints(nonce_anomaly_triggered)
    assert hints == {
        "events": Sequence[Mapping[str, object]],
        "wallet_fingerprint": str,
        "return": bool,
    }
    module_source = inspect.getsource(inspect.getmodule(nonce_anomaly_triggered))
    assert "reconciliation.admission" not in module_source
    assert "reconciliation.state" not in module_source


def _registration_for(wallet_fingerprint=WALLET):
    return AgentWalletRegistration(
        wallet_fingerprint, ISSUED_NS, ISSUED_NS + VALIDITY_NS,
    )


@pytest.mark.parametrize(
    "events",
    [
        [_freeze()],
        [_nonce_event(), _nonce_event(allocated=13, previous=12)],
    ],
)
def test_key_and_nonce_trigger_when_only_nonce_is_unsafe(events) -> None:
    registration = _registration_for()
    assert key_and_nonce_triggered(
        registration, events, now_ns=registration.expires_ns - 7 * DAY_NS,
    ) is True


def test_key_and_nonce_trigger_when_only_key_is_unsafe() -> None:
    registration = _registration_for()
    assert key_and_nonce_triggered(
        registration, [], now_ns=registration.expires_ns - 7 * DAY_NS + 1,
    ) is True


@pytest.mark.parametrize("triggered", [False, True])
def test_key_and_nonce_combines_both_detector_results(triggered) -> None:
    registration = _registration_for()
    now_ns = registration.expires_ns - 7 * DAY_NS + int(triggered)
    events = [_freeze()] if triggered else []
    assert key_and_nonce_triggered(registration, events, now_ns=now_ns) is triggered


def test_triggered_key_does_not_hide_a_malformed_nonce_stream() -> None:
    registration = _registration_for()
    malformed = _nonce_event(allocated=True)
    with pytest.raises(ValueError, match="allocated_nonce"):
        key_and_nonce_triggered(
            registration, [malformed], now_ns=registration.expires_ns,
        )


@pytest.mark.parametrize(
    ("registration", "nonce_events", "now_ns"),
    [
        (object(), [], ISSUED_NS),
        (_registration_for(), None, ISSUED_NS),
        (_registration_for(), [], True),
    ],
)
def test_key_and_nonce_requires_frozen_input_types(
    registration, nonce_events, now_ns,
) -> None:
    with pytest.raises(TypeError):
        key_and_nonce_triggered(registration, nonce_events, now_ns=now_ns)


def test_key_and_nonce_derives_the_wallet_fingerprint_from_registration() -> None:
    signature = inspect.signature(key_and_nonce_triggered)
    assert tuple(signature.parameters) == ("registration", "nonce_events", "now_ns")
    assert signature.parameters["now_ns"].kind is inspect.Parameter.KEYWORD_ONLY
    assert get_type_hints(key_and_nonce_triggered) == {
        "registration": AgentWalletRegistration,
        "nonce_events": Sequence[Mapping[str, object]],
        "now_ns": int,
        "return": bool,
    }
    registration = _registration_for(OTHER_WALLET)
    assert key_and_nonce_triggered(
        registration, [_freeze(fingerprint=OTHER_WALLET)],
        now_ns=registration.expires_ns - 7 * DAY_NS,
    ) is True


def _known_evidence(orders, positions, naked_notional, stablecoin):
    return kill_switch.KnownEvidence(
        orders=orders, positions=positions,
        naked_notional=naked_notional, stablecoin=stablecoin,
    )


@pytest.mark.parametrize(
    (
        "triggered", "orders_known", "positions_known", "naked_notional_known",
        "stablecoin_known", "reconciliation_consistency", "streak_triggered", "action",
    ),
    [
        (
            triggered, orders_known, positions_known, notional_known, fx_known,
            consistency, streak,
            "cancel_only_freeze"
            if not orders_known or not positions_known or not notional_known
            or not fx_known or consistency is None or streak
            or (triggered and consistency is False)
            else "flatten_and_stop" if triggered else "continue",
        )
        for triggered in (False, True)
        for orders_known in (False, True)
        for positions_known in (False, True)
        for notional_known in (False, True)
        for fx_known in (False, True)
        for consistency in (None, False, True)
        for streak in (False, True)
    ],
)
def test_kill_switch_decision_table(
    triggered: bool, orders_known: bool, positions_known: bool, naked_notional_known: bool,
    stablecoin_known: bool, reconciliation_consistency: bool | None,
    streak_triggered: bool, action: str,
) -> None:
    assert decide_kill_switch(
        triggered=triggered,
        known_evidence=_known_evidence(
            orders_known, positions_known, naked_notional_known, stablecoin_known),
        reconciliation_consistency=reconciliation_consistency,
        reconciliation_streak_triggered=streak_triggered,
    ) == KillSwitchDecision(action)


def test_pre_threshold_mismatch_continues_without_an_independent_trigger() -> None:
    assert decide_kill_switch(
        triggered=False, known_evidence=_known_evidence(True, True, True, True),
        reconciliation_consistency=False, reconciliation_streak_triggered=False,
    ) == KillSwitchDecision("continue")


def test_pre_threshold_mismatch_blocks_triggered_flattening() -> None:
    assert decide_kill_switch(
        triggered=True, known_evidence=_known_evidence(True, True, True, True),
        reconciliation_consistency=False, reconciliation_streak_triggered=False,
    ) == KillSwitchDecision("cancel_only_freeze")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("triggered", 1), ("triggered", None),
        ("reconciliation_consistency", 1), ("reconciliation_consistency", "true"),
        ("reconciliation_streak_triggered", 1),
        ("reconciliation_streak_triggered", None),
    ],
)
def test_kill_switch_decision_requires_exact_booleans(field, value) -> None:
    values = {
        "triggered": False,
        "known_evidence": _known_evidence(True, True, True, True),
        "reconciliation_consistency": True,
        "reconciliation_streak_triggered": False,
    }
    values[field] = value
    with pytest.raises(TypeError, match=field):
        decide_kill_switch(**values)


def test_kill_switch_decision_has_one_frozen_closed_action() -> None:
    assert [field.name for field in fields(KillSwitchDecision)] == ["action"]
    hints = get_type_hints(KillSwitchDecision)
    assert set(get_args(hints["action"])) == {
        "continue", "flatten_and_stop", "cancel_only_freeze",
    }
    decision = KillSwitchDecision("continue")
    with pytest.raises(FrozenInstanceError):
        decision.action = "flatten_and_stop"
    with pytest.raises(ValueError, match="action"):
        KillSwitchDecision("partial_flatten")

    signature = inspect.signature(decide_kill_switch)
    assert tuple(signature.parameters) == (
        "triggered", "known_evidence", "reconciliation_consistency",
        "reconciliation_streak_triggered",
    )
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )
    assert get_type_hints(decide_kill_switch)["return"] is KillSwitchDecision


def _exposure(*, state="naked", duration_exceeded=False):
    active_since = None if state == "flat" else ISSUED_NS
    return ExposureClock(state, ISSUED_NS, active_since, duration_exceeded)


@pytest.mark.parametrize(
    ("exposure", "amount", "maximum", "triggered"),
    [
        (_exposure(state="flat"), "0", "1000", False),
        (_exposure(), "1000", "1000", False),
        (_exposure(duration_exceeded=True), "0", "1000", True),
        (_exposure(), "1000.01", "1000", True),
        (_exposure(state="unknown", duration_exceeded=None), "0", "1000", True),
    ],
)
def test_exposure_kill_trigger_has_strict_independent_boundaries(
    exposure, amount, maximum, triggered,
) -> None:
    assert exposure_kill_trigger(
        exposure, Notional(Decimal(amount), "USDC"),
        max_naked_notional=Notional(Decimal(maximum), "USDC"),
    ) is triggered


def test_unknown_notional_triggers_even_with_healthy_exposure() -> None:
    assert exposure_kill_trigger(
        _exposure(state="flat"), None,
        max_naked_notional=Notional(Decimal("1000"), "USDC"),
    ) is True


@pytest.mark.parametrize("exposure,naked,maximum,error", [
    (object(), Notional(Decimal(0), "USDC"), Notional(Decimal(1), "USDC"), TypeError),
    (_exposure(), Decimal(0), Notional(Decimal(1), "USDC"), TypeError),
    (_exposure(), Notional(Decimal(0), "USDC"), None, TypeError),
    (_exposure(), Notional(Decimal(0), "USDT"), Notional(Decimal(1), "USDC"), ValueError),
])
def test_exposure_kill_trigger_validates_all_inputs_before_deciding(
    exposure, naked, maximum, error,
) -> None:
    with pytest.raises(error):
        exposure_kill_trigger(exposure, naked, max_naked_notional=maximum)
