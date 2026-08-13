import ast
import inspect

import pytest

from data.contracts import ROTATION_LEAD_NS, VALIDITY_NS
from execution.wallet import AgentWalletRegistration
from reconciliation.kill_switch import (
    KILL_SWITCH_KEY_EXPIRY_LEAD_NS,
    key_expiry_triggered,
)

DAY_NS = 86_400 * 1_000_000_000
ISSUED_NS = DAY_NS
REGISTRATION = AgentWalletRegistration(
    "a" * 64, ISSUED_NS, ISSUED_NS + VALIDITY_NS
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
