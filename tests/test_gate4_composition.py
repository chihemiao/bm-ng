import inspect
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal
from typing import get_type_hints

import pytest

from data.contracts import VALIDITY_NS
from execution.wallet import AgentWalletRegistration
from reconciliation.kill_switch import KillSwitchDecision, ReconciliationStreak
from reconciliation.kill_switch_composition import (
    KillSwitchSnapshot,
    KillSwitchSnapshotInputs,
    build_kill_switch_snapshot,
)
from reconciliation.ledger import BalanceLedger
from reconciliation.state import (
    CanonicalSet,
    ExpectedSurface,
    SurfaceEvidence,
    VenueEvidence,
    VenueExpectation,
)

REGISTRATION = AgentWalletRegistration("a" * 64, 1, 1 + VALIDITY_NS)
KEY_NOW = REGISTRATION.expires_ns - 7 * 86_400 * 1_000_000_000 + 1
NAMES = ("orders", "fills", "positions", "balances")


def _state(observed_ns=100, mismatch=False, unknown=False):
    surfaces = {
        name: SurfaceEvidence(
            observed_ns, 1, not unknown, False, 0, 0,
            CanonicalSet(f"{name}.state", 1, frozenset({f"{name}-state"})),
            CanonicalSet(f"{name}.identity", 1, frozenset({f"{name}-identity"})))
        for name in NAMES
    }
    venue = VenueEvidence(**surfaces)
    expected = {name: ExpectedSurface(value.entities, value.identities)
                for name, value in surfaces.items()}
    if mismatch:
        current = expected["orders"]
        state = replace(current.entities, fingerprints=frozenset({"other-state"}))
        expected["orders"] = replace(current, entities=state)
    ledger = BalanceLedger(
        0, observed_ns, (("USDC", "1"),), (("USDC", "1"),), (), frozenset(), True)
    expectation = VenueExpectation(
        **expected, frozen_intents=frozenset(), balance_ledger=ledger)
    return ({"hyperliquid": venue, "bybit": venue},
            {"hyperliquid": expectation, "bybit": expectation})


def _inputs(observed_ns=100, **changes):
    venues, expectations = _state(
        observed_ns, changes.pop("mismatch", False), changes.pop("unknown", False))
    values = dict(
        registration=REGISTRATION, nonce_events=(), previous_streak=None,
        streak_threshold=3, venues=venues, expectations=expectations,
        delta=Decimal(0), reconciliation_observed_ns=observed_ns,
        now_ns=observed_ns, max_age_ns=0)
    return KillSwitchSnapshotInputs(**(values | changes))


@pytest.mark.parametrize(
    "observed,mismatch,previous,now,action,count",
    [
        (100, False, None, 100, "continue", 0),
        (100, True, None, 100, "continue", 1),
        (101, True, ReconciliationStreak(count=1, last_observed_ns=100), 101, "continue", 2),
        (102, True, ReconciliationStreak(count=2, last_observed_ns=101), 102,
         "cancel_only_freeze", 3),
        (KEY_NOW, False, None, KEY_NOW, "flatten_and_stop", 0),
        (KEY_NOW, True, None, KEY_NOW, "cancel_only_freeze", 1),
    ])
def test_snapshot_composes_all_current_inputs(observed, mismatch, previous, now, action, count):
    result = build_kill_switch_snapshot(_inputs(
        observed, mismatch=mismatch, previous_streak=previous, now_ns=now))
    assert (result.decision.action, result.reconciliation_streak.count) == (action, count)
    assert result.reconciliation_consistency is (not mismatch)


def test_unknown_preserves_reached_streak_identity_and_cancels():
    previous = ReconciliationStreak(count=3, last_observed_ns=99)
    result = build_kill_switch_snapshot(
        _inputs(100, unknown=True, previous_streak=previous))
    assert result.reconciliation_streak is previous
    assert (result.reconciliation_consistency, result.decision) == (
        None, KillSwitchDecision("cancel_only_freeze"))


@pytest.mark.parametrize("delta", [None, Decimal("NaN"), Decimal("Infinity")])
def test_position_unknown_or_nonfinite_delta_is_fail_closed(delta):
    if delta is None:
        result = build_kill_switch_snapshot(_inputs(delta=delta))
        assert result.decision.action == "cancel_only_freeze"
    else:
        with pytest.raises(ValueError, match="delta"):
            build_kill_switch_snapshot(_inputs(delta=delta))


def test_cycle_replay_is_idempotent_and_contradiction_is_rejected():
    first = build_kill_switch_snapshot(_inputs(100, mismatch=True))
    replay = build_kill_switch_snapshot(
        _inputs(100, mismatch=True, previous_streak=first.reconciliation_streak))
    assert replay.reconciliation_streak is first.reconciliation_streak
    with pytest.raises(ValueError, match="same observed_ns"):
        build_kill_switch_snapshot(
            _inputs(100, previous_streak=first.reconciliation_streak))


@pytest.mark.parametrize("changes", [
    {"unknown": True, "streak_threshold": 0},
    {"unknown": True, "previous_streak": object()},
    {"previous_streak": ReconciliationStreak(count=1, last_observed_ns=101)},
    {"reconciliation_observed_ns": 99}, {"now_ns": 99},
])
def test_invalid_state_is_never_short_circuited(changes):
    with pytest.raises((TypeError, ValueError)):
        build_kill_switch_snapshot(_inputs(**changes))


def test_snapshot_contract_is_narrow_frozen_slotted_and_keyword_only():
    assert [field.name for field in fields(KillSwitchSnapshotInputs)] == [
        "registration", "nonce_events", "previous_streak", "streak_threshold",
        "venues", "expectations", "delta", "reconciliation_observed_ns", "now_ns", "max_age_ns"]
    assert [field.name for field in fields(KillSwitchSnapshot)] == [
        "decision", "reconciliation_streak", "reconciliation_consistency"]
    assert all(value.kind is inspect.Parameter.KEYWORD_ONLY
               for value in inspect.signature(KillSwitchSnapshotInputs).parameters.values())
    result = build_kill_switch_snapshot(_inputs())
    with pytest.raises(FrozenInstanceError):
        result.reconciliation_consistency = False
    assert KillSwitchSnapshot.__slots__
    assert tuple(inspect.signature(build_kill_switch_snapshot).parameters) == ("inputs",)
    assert get_type_hints(build_kill_switch_snapshot)["return"] is KillSwitchSnapshot
