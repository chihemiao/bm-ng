from dataclasses import asdict, fields
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


@pytest.mark.parametrize(
    ("name", "field_names"),
    [
        ("HLCancelTarget", ["coin", "oid"]),
        ("HLCancelBatch", ["targets"]),
        ("BybitCancelScope", ["category", "settle_coin"]),
    ],
)
def test_cancel_request_values_are_closed_keyword_only(name, field_names):
    value_type = getattr(cancel, name)
    assert [field.name for field in fields(value_type)] == field_names
    assert value_type.__dataclass_params__.frozen
    assert value_type.__slots__ == tuple(field_names)
    assert all(
        value.kind is Parameter.KEYWORD_ONLY
        for value in signature(value_type).parameters.values()
    )


def test_hl_cancel_batch_keeps_wire_order_extras_and_duplicates():
    batch = cancel.build_hl_cancel_batch(
        [
            {"coin": "ETH", "oid": 2, "ignored": "wire detail"},
            {"coin": "BTC", "oid": 0},
            {"coin": "ETH", "oid": 2},
        ]
    )
    assert batch == cancel.HLCancelBatch(
        targets=(
            cancel.HLCancelTarget(coin="ETH", oid=2),
            cancel.HLCancelTarget(coin="BTC", oid=0),
            cancel.HLCancelTarget(coin="ETH", oid=2),
        )
    )


def test_hl_empty_cancel_payload_is_an_empty_batch():
    assert cancel.build_hl_cancel_batch([]) == cancel.HLCancelBatch(targets=())


def test_hl_cancel_oid_accepts_the_uint64_upper_bound():
    batch = cancel.build_hl_cancel_batch([{"coin": "BTC", "oid": 2**64 - 1}])
    assert batch.targets[0].oid == 2**64 - 1


@pytest.mark.parametrize("oid", [True, "1", 1.0])
def test_hl_cancel_oid_rejects_non_integer_types(oid):
    with pytest.raises(TypeError, match="oid"):
        cancel.build_hl_cancel_batch([{"coin": "BTC", "oid": oid}])


@pytest.mark.parametrize("oid", [-1, 2**64])
def test_hl_cancel_oid_rejects_values_outside_uint64(oid):
    with pytest.raises(ValueError, match="oid"):
        cancel.build_hl_cancel_batch([{"coin": "BTC", "oid": oid}])


@pytest.mark.parametrize(
    ("coin", "error"),
    [("", ValueError), ("SOL", ValueError), (7, TypeError)],
)
def test_hl_cancel_coin_is_a_typed_t0a_closed_set(coin, error):
    with pytest.raises(error, match="coin"):
        cancel.build_hl_cancel_batch([{"coin": coin, "oid": 1}])


def test_hl_cancel_payload_must_be_a_list_of_dicts():
    with pytest.raises(TypeError, match="payload"):
        cancel.build_hl_cancel_batch({})
    with pytest.raises(TypeError, match="row"):
        cancel.build_hl_cancel_batch([[]])


@pytest.mark.parametrize("missing", ["coin", "oid"])
def test_hl_cancel_missing_field_rejects_the_entire_batch(missing):
    malformed = {"coin": "ETH", "oid": 2}
    malformed.pop(missing)
    with pytest.raises(ValueError, match="missing"):
        cancel.build_hl_cancel_batch(
            [{"coin": "BTC", "oid": 1}, malformed, {"coin": "ETH", "oid": 3}]
        )


def test_bybit_cancel_scope_is_exactly_the_t0a_usdt_linear_scope():
    scope = cancel.BybitCancelScope(category="linear", settle_coin="USDT")
    assert asdict(scope) == {"category": "linear", "settle_coin": "USDT"}


@pytest.mark.parametrize(
    "values",
    [
        {"category": "spot", "settle_coin": "USDT"},
        {"category": "linear", "settle_coin": "USDC"},
    ],
)
def test_bybit_cancel_scope_rejects_other_domains(values):
    with pytest.raises(ValueError):
        cancel.BybitCancelScope(**values)


@pytest.mark.parametrize(
    "values",
    [
        {"category": 1, "settle_coin": "USDT"},
        {"category": "linear", "settle_coin": 1},
    ],
)
def test_bybit_cancel_scope_rejects_non_string_fields(values):
    with pytest.raises(TypeError):
        cancel.BybitCancelScope(**values)


def test_hl_cancel_binding_calls_once_with_exact_ordered_wire_rows():
    calls, result = [], object()

    def bulk_cancel(rows):
        calls.append(rows)
        return result

    batch = cancel.HLCancelBatch(
        targets=(
            cancel.HLCancelTarget(coin="ETH", oid=2),
            cancel.HLCancelTarget(coin="BTC", oid=1),
            cancel.HLCancelTarget(coin="ETH", oid=2),
            cancel.HLCancelTarget(coin="BTC", oid=3),
        )
    )
    transport = cancel.bind_hl_cancel(batch, bulk_cancel)
    assert calls == []
    assert transport() is result
    assert calls == [[
        {"coin": "ETH", "oid": 2},
        {"coin": "BTC", "oid": 1},
        {"coin": "ETH", "oid": 2},
        {"coin": "BTC", "oid": 3},
    ]]


def test_hl_empty_cancel_binding_still_calls_bulk_cancel_once():
    calls = []
    transport = cancel.bind_hl_cancel(
        cancel.HLCancelBatch(targets=()), lambda rows: calls.append(rows)
    )
    assert transport() is None
    assert calls == [[]]


def test_bybit_cancel_binding_uses_exact_official_wire_kwargs_once():
    calls, result = [], object()

    def cancel_all(**kwargs):
        calls.append(kwargs)
        return result

    scope = cancel.BybitCancelScope(category="linear", settle_coin="USDT")
    transport = cancel.bind_bybit_cancel(scope, cancel_all)
    assert calls == []
    assert transport() is result
    assert calls == [{"category": "linear", "settleCoin": "USDT"}]


@pytest.mark.parametrize(
    ("binder_name", "value", "invalid_position"),
    [
        ("bind_hl_cancel", cancel.HLCancelBatch(targets=()), "value"),
        (
            "bind_bybit_cancel",
            cancel.BybitCancelScope(category="linear", settle_coin="USDT"),
            "value",
        ),
        ("bind_hl_cancel", cancel.HLCancelBatch(targets=()), "callable"),
        (
            "bind_bybit_cancel",
            cancel.BybitCancelScope(category="linear", settle_coin="USDT"),
            "callable",
        ),
    ],
)
def test_cancel_binding_preflights_both_dependencies(binder_name, value, invalid_position):
    calls = []

    def venue_call(*args, **kwargs):
        calls.append((args, kwargs))

    values = [value, venue_call]
    values[0 if invalid_position == "value" else 1] = object()
    with pytest.raises(TypeError):
        getattr(cancel, binder_name)(*values)
    assert calls == []


@pytest.mark.parametrize("binder_name", ["bind_hl_cancel", "bind_bybit_cancel"])
def test_cancel_binding_propagates_the_same_base_exception(binder_name):
    error = KeyboardInterrupt(binder_name)

    def venue_call(*args, **kwargs):
        raise error

    value = (
        cancel.HLCancelBatch(targets=())
        if binder_name == "bind_hl_cancel"
        else cancel.BybitCancelScope(category="linear", settle_coin="USDT")
    )
    transport = getattr(cancel, binder_name)(value, venue_call)
    with pytest.raises(KeyboardInterrupt) as caught:
        transport()
    assert caught.value is error
