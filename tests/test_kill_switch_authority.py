from inspect import getsource

import pytest

import reconciliation.promotion as promotion
from data.contracts import ContractError, validate_envelope
from execution.writer import WriterIdentity, WriterLease, WriterLeaseError
from reconciliation.kill_switch import KillSwitchDecision

REASON = "writer_demoted:kill_switch:flatten_and_stop"


def _lease(root, mode="risk_increasing", recorder=None):
    recorded = []
    lease = WriterLease.acquire(
        root,
        WriterIdentity("test-account", "writer-one", "b" * 64, "boot-one"),
        recorded.append if recorder is None else recorder,
        acquired_ns=90,
    )
    lease._authority = lease.authority._replace(mode=mode)
    recorded.clear()
    return lease, recorded


def _event(decision):
    return {
        "schema_ver": 1, "event_kind": "decision",
        "payload_schema": "writer_lease_decision", "venue": "hyperliquid",
        "conn_id": "writer-one", "boot_id": "boot-one",
        "recv_wall_ns": 101, "recv_mono_ns": 101, "source": "writer_lease",
        "seq_within_boot": 1, "payload": decision._asdict(),
    }


def test_flatten_decision_enters_mode_before_recording_canonical_evidence(tmp_path):
    holder, observed, recorded = {}, [], []

    def record(decision):
        recorded.append(decision)
        if decision.action == "demote":
            observed.append(holder["lease"].authority.mode)

    lease, _ = _lease(tmp_path, recorder=record)
    holder["lease"] = lease
    recorded.clear()
    authority = promotion.demote_kill_switch_flatten(
        lease, KillSwitchDecision("flatten_and_stop"), now_ns=100)
    assert authority.mode == lease.authority.mode == "flatten_only"
    assert observed == ["flatten_only"] and len(recorded) == 1
    assert (recorded[0].action, recorded[0].outcome, recorded[0].reason) == (
        "demote", "flatten_only", REASON)
    assert validate_envelope(_event(recorded[0]))
    source = getsource(promotion.demote_kill_switch_flatten)
    assert source.count("demote_to_flatten_only(") == 1
    lease.release()


@pytest.mark.parametrize("mode", ["pending_reconciliation", "cancel_only", "flatten_only"])
def test_only_risk_increasing_can_enter_flatten_only(tmp_path, mode):
    lease, recorded = _lease(tmp_path, mode)
    with pytest.raises(WriterLeaseError, match="risk increasing"):
        lease.demote_to_flatten_only(demotion_ns=100, reason=REASON)
    assert lease.authority.mode == mode and recorded == []
    lease.release()


@pytest.mark.parametrize("decision", [KillSwitchDecision("continue"),
                                       KillSwitchDecision("cancel_only_freeze"), object()])
def test_non_flatten_decisions_cannot_enter_or_record(tmp_path, decision):
    lease, recorded = _lease(tmp_path)
    with pytest.raises((TypeError, ValueError)):
        promotion.demote_kill_switch_flatten(lease, decision, now_ns=100)
    assert lease.authority.mode == "risk_increasing" and recorded == []
    lease.release()


def test_record_failure_keeps_flatten_only_applied(tmp_path):
    failure = OSError("decision stream unavailable")

    def fail(decision):
        if decision.action == "demote":
            raise failure

    lease, _ = _lease(tmp_path, recorder=fail)
    with pytest.raises(WriterLeaseError, match="applied.*evidence") as caught:
        promotion.demote_kill_switch_flatten(
            lease, KillSwitchDecision("flatten_and_stop"), now_ns=100)
    assert caught.value.__cause__ is failure and lease.authority.mode == "flatten_only"
    lease.release()


def test_flatten_completion_enters_cancel_only_before_recording(tmp_path):
    holder, observed, recorded = {}, [], []

    def record(decision):
        recorded.append(decision)
        if decision.action == "demote":
            observed.append(holder["lease"].authority.mode)

    lease, _ = _lease(tmp_path, "flatten_only", record)
    holder["lease"] = lease
    recorded.clear()
    authority = promotion.demote_kill_switch_complete(lease, now_ns=100)
    assert authority.mode == lease.authority.mode == "cancel_only"
    assert observed == ["cancel_only"] and len(recorded) == 1
    assert recorded[0].reason == "writer_demoted:kill_switch:flatten_complete"
    assert validate_envelope(_event(recorded[0]))
    source = getsource(promotion.demote_kill_switch_complete)
    assert source.count("_require_flatten_only(") == source.count("demote_to_cancel_only(") == 1
    lease.release()


@pytest.mark.parametrize("mode", ["pending_reconciliation", "cancel_only", "risk_increasing"])
def test_only_flatten_only_can_complete_flattening(tmp_path, mode):
    lease, recorded = _lease(tmp_path, mode)
    with pytest.raises(WriterLeaseError, match="flatten only"):
        promotion.demote_kill_switch_complete(lease, now_ns=100)
    assert lease.authority.mode == mode and recorded == []
    lease.release()


def test_completion_record_failure_keeps_cancel_only_applied(tmp_path):
    failure = OSError("decision stream unavailable")

    def fail(decision):
        if decision.action == "demote":
            raise failure

    lease, _ = _lease(tmp_path, "flatten_only", fail)
    with pytest.raises(WriterLeaseError, match="applied.*evidence") as caught:
        promotion.demote_kill_switch_complete(lease, now_ns=100)
    assert caught.value.__cause__ is failure and lease.authority.mode == "cancel_only"
    lease.release()


def test_flatten_only_can_freeze_to_cancel_only_and_revoke_reduce_only(tmp_path):
    lease, recorded = _lease(tmp_path)
    promotion.demote_kill_switch_flatten(
        lease, KillSwitchDecision("flatten_and_stop"), now_ns=100)
    recorded.clear()
    promotion.demote_kill_switch_freeze(
        lease, KillSwitchDecision("cancel_only_freeze"), now_ns=101)
    assert lease.authority.mode == "cancel_only" and len(recorded) == 1
    assert validate_envelope(_event(recorded[0]))
    with pytest.raises(WriterLeaseError, match="not authorized"):
        lease.authorize("reduce_only")
    lease.release()


def test_schema_rejects_every_other_flatten_only_reason(tmp_path):
    lease, recorded = _lease(tmp_path)
    lease.demote_to_flatten_only(demotion_ns=100, reason=REASON)
    for reason in ("writer_demoted:kill_switch:flatten", REASON + ":detail"):
        event = _event(recorded[0]._replace(reason=reason))
        with pytest.raises(ContractError, match="writer decision combination"):
            validate_envelope(event)
    lease.release()
