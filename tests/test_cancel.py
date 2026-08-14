from dataclasses import fields
from inspect import Parameter, getsource, signature

import pytest

import execution.cancel as cancel
from execution.writer import WriterIdentity, WriterLease, WriterLeaseError


def _lease(root, mode="cancel_only"):
    lease = WriterLease.acquire(
        root,
        WriterIdentity("test-account", "writer-one", "b" * 64, "boot-one"),
        [].append,
        acquired_ns=90,
    )
    lease._authority = lease.authority._replace(mode=mode)
    return lease


def _transport(label, result, calls):
    def transport():
        calls.append(label)
        if isinstance(result, BaseException):
            raise result
        return result

    return transport


def test_pair_cancel_outcome_is_a_closed_keyword_only_value():
    assert [field.name for field in fields(cancel.PairCancelOutcome)] == [
        "hyperliquid", "bybit"]
    assert cancel.PairCancelOutcome.__dataclass_params__.frozen
    assert cancel.PairCancelOutcome.__slots__ == ("hyperliquid", "bybit")
    parameters = signature(cancel.PairCancelOutcome).parameters.values()
    assert all(value.kind is Parameter.KEYWORD_ONLY for value in parameters)


@pytest.mark.parametrize(
    "mode", ["pending_reconciliation", "cancel_only", "flatten_only", "risk_increasing"]
)
def test_every_writer_mode_can_cancel_both_venues_in_fixed_order(tmp_path, mode):
    calls, hl_result, bybit_result = [], object(), object()
    lease = _lease(tmp_path, mode)
    outcome = cancel.cancel_pair_orders(
        lease=lease,
        hyperliquid_transport=_transport("hyperliquid", hl_result, calls),
        bybit_transport=_transport("bybit", bybit_result, calls),
    )
    assert calls == ["hyperliquid", "bybit"]
    assert outcome.hyperliquid is hl_result and outcome.bybit is bybit_result
    lease.release()


@pytest.mark.parametrize(
    ("hl_fails", "bybit_fails"),
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_each_transport_result_or_base_exception_keeps_its_identity(
    tmp_path, hl_fails, bybit_fails,
):
    calls = []
    hl_result = KeyboardInterrupt("hl") if hl_fails else object()
    bybit_result = SystemExit("bybit") if bybit_fails else object()
    lease = _lease(tmp_path)
    outcome = cancel.cancel_pair_orders(
        lease=lease,
        hyperliquid_transport=_transport("hyperliquid", hl_result, calls),
        bybit_transport=_transport("bybit", bybit_result, calls),
    )
    assert calls == ["hyperliquid", "bybit"]
    assert outcome.hyperliquid is hl_result and outcome.bybit is bybit_result
    lease.release()


@pytest.mark.parametrize("invalid", ["lease", "hyperliquid_transport", "bybit_transport"])
def test_all_dependencies_fail_preflight_before_transport(tmp_path, invalid):
    calls, lease = [], _lease(tmp_path)
    values = {
        "lease": lease,
        "hyperliquid_transport": _transport("hyperliquid", object(), calls),
        "bybit_transport": _transport("bybit", object(), calls),
    }
    values[invalid] = object()
    with pytest.raises(TypeError):
        cancel.cancel_pair_orders(**values)
    assert calls == []
    lease.release()


def test_callable_preflight_precedes_authority_revalidation(tmp_path):
    lease = _lease(tmp_path, "risk_increasing")
    lease.release()
    with pytest.raises(TypeError, match="bybit_transport"):
        cancel.cancel_pair_orders(
            lease=lease, hyperliquid_transport=lambda: None, bybit_transport=None)


def test_released_owner_cannot_reach_either_transport(tmp_path):
    calls, lease = [], _lease(tmp_path, "risk_increasing")
    lease.release()
    with pytest.raises(WriterLeaseError, match="authority"):
        cancel.cancel_pair_orders(
            lease=lease,
            hyperliquid_transport=_transport("hyperliquid", object(), calls),
            bybit_transport=_transport("bybit", object(), calls),
        )
    assert calls == []


def test_orchestrator_has_one_authority_gate_and_no_kill_switch_policy():
    module_source = getsource(cancel)
    source = getsource(cancel.cancel_pair_orders)
    assert source.count('authorize("cancel_all")') == 1
    assert source.count("run(hyperliquid_transport)") == 1
    assert source.count("run(bybit_transport)") == 1
    assert "KillSwitchDecision" not in module_source
    assert "reconciliation.kill_switch" not in module_source
