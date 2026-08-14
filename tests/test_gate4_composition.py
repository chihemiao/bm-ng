import ast
import inspect
from dataclasses import fields, replace
from decimal import Decimal

import pytest

import reconciliation.promotion as promotion
from data.contracts import VALIDITY_NS, ContractError, validate_envelope
from execution.wallet import AgentWalletRegistration
from execution.writer import WriterIdentity, WriterLease, WriterLeaseError
from reconciliation.exposure import ExposureClock
from reconciliation.fx import Notional
from reconciliation.kill_switch import KillSwitchDecision, ReconciliationStreak
from reconciliation.kill_switch_composition import (
    KillSwitchSnapshot,
    KillSwitchSnapshotInputs,
)
from reconciliation.kill_switch_composition import build_kill_switch_snapshot as build_snapshot
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
def _state(observed_ns=100, mismatch=False, unknown=False):
    surfaces = {
        name: SurfaceEvidence(
            observed_ns, 1, not unknown, False, 0, 0,
            CanonicalSet(f"{name}.state", 1, frozenset({f"{name}-state"})),
            CanonicalSet(f"{name}.identity", 1, frozenset({f"{name}-identity"})))
        for name in ("orders", "fills", "positions", "balances")
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
        delta=Decimal(0), previous_exposure=None, delta_tolerance=Decimal("0.01"),
        max_naked_ns=10, naked_notional=Notional(Decimal(0), "USDC"),
        max_naked_notional=Notional(Decimal("1000"), "USDC"),
        reconciliation_observed_ns=observed_ns,
        now_ns=observed_ns, max_age_ns=0)
    return KillSwitchSnapshotInputs(**(values | changes))


@pytest.mark.parametrize(
    "obs,mismatch,previous,now,age,action,count",
    [
        (100, False, None, 100, 0, "continue", 0),
        (100, True, None, 101, 1, "continue", 1),
        (101, True, ReconciliationStreak(count=1, last_observed_ns=100), 101, 0, "continue", 2),
        (102, True, ReconciliationStreak(count=2, last_observed_ns=101), 102,
         0, "cancel_only_freeze", 3),
        (KEY_NOW, False, None, KEY_NOW, 0, "flatten_and_stop", 0),
        (KEY_NOW, True, None, KEY_NOW, 0, "cancel_only_freeze", 1),
        (103, False, ReconciliationStreak(count=3, last_observed_ns=102), 103, 0, "continue", 0),
    ])
def test_snapshot_composes_all_current_inputs(obs, mismatch, previous, now, age, action, count):
    result = build_snapshot(_inputs(
        obs, mismatch=mismatch, previous_streak=previous, now_ns=now, max_age_ns=age))
    streak = result.reconciliation_streak
    assert (result.decision.action, streak.count, streak.last_observed_ns) == (action, count, obs)
    assert result.reconciliation_consistency is (not mismatch)


def test_unknown_preserves_reached_streak_identity_and_cancels():
    previous = ReconciliationStreak(count=3, last_observed_ns=99)
    result = build_snapshot(
        _inputs(100, unknown=True, previous_streak=previous))
    assert result.reconciliation_streak is previous
    assert (result.reconciliation_consistency, result.decision) == (
        None, KillSwitchDecision("cancel_only_freeze"))


@pytest.mark.parametrize("delta", [None, Decimal("NaN"), Decimal("Infinity")])
def test_position_unknown_or_nonfinite_delta_is_fail_closed(delta):
    if delta is None:
        result = build_snapshot(_inputs(delta=delta))
        assert result.decision.action == "cancel_only_freeze"
    else:
        with pytest.raises(ValueError, match="delta"):
            build_snapshot(_inputs(delta=delta))


@pytest.mark.parametrize("changes,action,state,exceeded", [
    ({}, "continue", "flat", False),
    ({"delta": Decimal("0.01")}, "continue", "flat", False),
    ({"delta": Decimal("0.02"),
      "previous_exposure": ExposureClock("naked", 89, 89, False)},
     "flatten_and_stop", "naked", True),
    ({"naked_notional": Notional(Decimal("1001"), "USDC")},
     "flatten_and_stop", "flat", False),
    ({"naked_notional": Notional(Decimal("1000"), "USDC")},
     "continue", "flat", False),
    ({"delta": None}, "cancel_only_freeze", "unknown", None),
    ({"naked_notional": None}, "cancel_only_freeze", "flat", False),
    ({"mismatch": True, "naked_notional": Notional(Decimal("1001"), "USDC")},
     "cancel_only_freeze", "flat", False),
])
def test_snapshot_composes_exposure_into_the_existing_decision_table(
    changes, action, state, exceeded,
):
    result = build_snapshot(_inputs(**changes))
    assert (result.decision.action, result.exposure.state,
            result.exposure.duration_exceeded) == (action, state, exceeded)


def test_exposure_clock_uses_the_reconciliation_cycle_not_decision_time():
    result = build_snapshot(_inputs(100, now_ns=101, max_age_ns=1))
    assert result.exposure.observed_ns == 100


def test_triggered_key_does_not_hide_malformed_exposure_inputs():
    with pytest.raises(ValueError, match="quote"):
        build_snapshot(_inputs(
            KEY_NOW, naked_notional=Notional(Decimal(0), "USDT")))


def test_positions_knowability_reuses_the_derived_exposure_state():
    source = inspect.getsource(build_snapshot)
    assert source.count("delta_state(") == 1
    assert 'positions_known = exposure_state.state != "unknown"' in source
    assert "naked_notional_known=inputs.naked_notional is not None" in source


def test_cycle_replay_is_idempotent_and_contradiction_is_rejected():
    first = build_snapshot(_inputs(100, mismatch=True))
    replay = build_snapshot(
        _inputs(100, mismatch=True, previous_streak=first.reconciliation_streak))
    assert replay.reconciliation_streak is first.reconciliation_streak
    with pytest.raises(ValueError, match="same observed_ns"):
        build_snapshot(
            _inputs(100, previous_streak=first.reconciliation_streak))


@pytest.mark.parametrize("changes", [
    {"unknown": True, "streak_threshold": 0},
    {"unknown": True, "streak_threshold": True},
    {"unknown": True, "previous_streak": object()},
    {"previous_streak": ReconciliationStreak(count=1, last_observed_ns=101)},
    {"reconciliation_observed_ns": 99}, {"reconciliation_observed_ns": True},
    {"now_ns": 99}, {"now_ns": 0},
    {"previous_exposure": object()}, {"delta_tolerance": Decimal("NaN")},
    {"max_naked_ns": -1}, {"naked_notional": Decimal(0)},
    {"max_naked_notional": None},
])
def test_invalid_state_is_never_short_circuited(changes):
    with pytest.raises((TypeError, ValueError)):
        build_snapshot(_inputs(**changes))


def test_snapshot_contract_is_narrow_frozen_slotted_and_keyword_only():
    assert [field.name for field in fields(KillSwitchSnapshotInputs)] == [
        "registration", "nonce_events", "previous_streak", "streak_threshold",
        "venues", "expectations", "delta", "previous_exposure", "delta_tolerance",
        "max_naked_ns", "naked_notional", "max_naked_notional",
        "reconciliation_observed_ns", "now_ns", "max_age_ns"]
    assert [field.name for field in fields(KillSwitchSnapshot)] == [
        "decision", "reconciliation_streak", "reconciliation_consistency", "exposure"]
    parameters = inspect.signature(KillSwitchSnapshotInputs).parameters.values()
    assert all(value.kind is inspect.Parameter.KEYWORD_ONLY for value in parameters)
    assert KillSwitchSnapshot.__dataclass_params__.frozen and KillSwitchSnapshot.__slots__
    assert tuple(inspect.signature(build_snapshot).parameters) == ("inputs",)
    assert inspect.get_annotations(build_snapshot)["return"] is KillSwitchSnapshot
    source = inspect.getsource(build_snapshot)
    assert all(text in source for text in (
        "surface_is_authoritative", "evidence.orders", "now_ns=now"))


def _lease(root, mode, recorder):
    identity = WriterIdentity("hyperliquid:test", "writer-one", "b" * 64, "boot-one")
    lease = WriterLease.acquire(root, identity, recorder, acquired_ns=100)
    lease._authority = lease.authority._replace(mode=mode)
    return lease


def _writer_event(reason):
    return {
        "schema_ver": 1, "event_kind": "decision", "payload_schema": "writer_lease_decision",
        "venue": "hyperliquid", "conn_id": "writer-one", "boot_id": "boot-one",
        "recv_wall_ns": 101, "recv_mono_ns": 101, "source": "writer_lease",
        "seq_within_boot": 1, "payload": {
            "action": "demote", "outcome": "cancel_only", "reason": reason,
            "account_digest": "a" * 64, "instance_id": "writer-one",
            "wallet_fingerprint": "b" * 64, "boot_id": "boot-one", "lease_epoch": 1,
            "lock_path_digest": "c" * 64, "prior_epoch_valid": True}}


def test_gate4_freeze_demotes_before_recording_canonical_evidence(tmp_path):
    recorded = []
    lease = _lease(tmp_path, "risk_increasing", recorded.append)
    recorded.clear()
    authority = promotion.demote_kill_switch_freeze(
        lease, KillSwitchDecision("cancel_only_freeze"), now_ns=101)
    assert authority == lease.authority and authority.mode == "cancel_only"
    assert len(recorded) == 1
    assert recorded[0].reason == "writer_demoted:kill_switch:cancel_only_freeze"
    assert validate_envelope(_writer_event(recorded[0].reason))
    with pytest.raises(WriterLeaseError, match="not authorized"):
        lease.authorize("submit")
    lease.release()


@pytest.mark.parametrize("decision", [KillSwitchDecision("continue"),
                                        KillSwitchDecision("flatten_and_stop"), object()])
def test_non_freeze_decisions_cannot_demote_or_record(tmp_path, decision):
    recorded = []
    lease = _lease(tmp_path, "risk_increasing", recorded.append)
    recorded.clear()
    with pytest.raises((TypeError, ValueError)):
        promotion.demote_kill_switch_freeze(lease, decision, now_ns=101)
    assert lease.authority.mode == "risk_increasing" and recorded == []
    lease.release()


@pytest.mark.parametrize("mode", ["pending_reconciliation", "cancel_only"])
def test_gate4_freeze_is_idempotent_outside_risk_increasing(tmp_path, mode):
    recorded = []
    lease = _lease(tmp_path, mode, recorded.append)
    recorded.clear()
    promotion.demote_kill_switch_freeze(
        lease, KillSwitchDecision("cancel_only_freeze"), now_ns=101)
    assert lease.authority.mode == mode and recorded == []
    lease.release()


@pytest.mark.parametrize("now_ns,error", [(True, TypeError), (0, ValueError)])
def test_gate4_freeze_rejects_invalid_time_before_state_change(tmp_path, now_ns, error):
    recorded = []
    lease = _lease(tmp_path, "risk_increasing", recorded.append)
    recorded.clear()
    with pytest.raises(error):
        promotion.demote_kill_switch_freeze(
            lease, KillSwitchDecision("cancel_only_freeze"), now_ns=now_ns)
    assert lease.authority.mode == "risk_increasing" and recorded == []
    lease.release()


@pytest.mark.parametrize("reason", [
    "writer_demoted:kill_switch:continue", "writer_demoted:kill_switch:flatten_and_stop",
    "writer_demoted:kill_switch:cancel_only_freeze:detail",
    "writer_demoted:continuous_admission:pair_unknown,kill_switch:cancel_only_freeze",
    "writer_demoted:kill_switch:cancel_only_freeze,kill_switch:cancel_only_freeze",
])
def test_schema_rejects_every_other_gate4_demotion_reason(reason):
    with pytest.raises(ContractError, match="writer decision combination"):
        validate_envelope(_writer_event(reason))


def test_gate4_freeze_preserves_applied_demotion_when_evidence_fails(tmp_path):
    failure = OSError("decision stream unavailable")
    def recorder(decision):
        if decision.action == "demote":
            raise failure
    lease = _lease(tmp_path, "risk_increasing", recorder)
    with pytest.raises(WriterLeaseError, match="demotion applied.*evidence") as caught:
        promotion.demote_kill_switch_freeze(
            lease, KillSwitchDecision("cancel_only_freeze"), now_ns=101)
    assert caught.value.__cause__ is failure and lease.authority.mode == "cancel_only"
    lease.release()


def test_gate4_freeze_delegates_only_to_the_authority_demotion_atom():
    tree = ast.parse(inspect.getsource(promotion.demote_kill_switch_freeze))
    calls = {node.func.id if isinstance(node.func, ast.Name) else node.func.attr
             for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert calls == {"_demote", "TypeError", "ValueError", "isinstance"}
