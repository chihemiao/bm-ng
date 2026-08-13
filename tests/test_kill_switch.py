import ast
import inspect
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError, fields
from typing import get_args, get_type_hints

import pytest

from data.contracts import ROTATION_LEAD_NS, VALIDITY_NS
from execution.wallet import AgentWalletRegistration
from reconciliation.kill_switch import (
    KILL_SWITCH_KEY_EXPIRY_LEAD_NS,
    KillSwitchDecision,
    decide_kill_switch,
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


@pytest.mark.parametrize(
    ("triggered", "orders_known", "positions_known", "action"),
    [
        (False, True, True, "continue"),
        (True, True, True, "flatten_and_stop"),
        (False, False, True, "cancel_only_freeze"),
        (True, False, True, "cancel_only_freeze"),
        (False, True, False, "cancel_only_freeze"),
        (True, True, False, "cancel_only_freeze"),
        (False, False, False, "cancel_only_freeze"),
        (True, False, False, "cancel_only_freeze"),
    ],
)
def test_kill_switch_decision_table(
    triggered: bool, orders_known: bool, positions_known: bool, action: str,
) -> None:
    assert decide_kill_switch(
        triggered=triggered,
        orders_known=orders_known,
        positions_known=positions_known,
    ) == KillSwitchDecision(action)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("triggered", 1), ("triggered", None),
        ("orders_known", 1), ("orders_known", None),
        ("positions_known", 1), ("positions_known", None),
    ],
)
def test_kill_switch_decision_requires_exact_booleans(field, value) -> None:
    values = {"triggered": False, "orders_known": True, "positions_known": True}
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
    assert tuple(signature.parameters) == ("triggered", "orders_known", "positions_known")
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )
    assert get_type_hints(decide_kill_switch)["return"] is KillSwitchDecision
