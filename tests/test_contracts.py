import ast
from copy import deepcopy
from pathlib import Path

import pytest

from data import contracts
from data.contracts import (
    PAYLOAD_SCHEMAS,
    ContractError,
    bybit_update_gap,
    checksum_matches,
    monotonic_elapsed_ns,
    validate_envelope,
    validate_manifest,
)

LEGACY_PAYLOAD_SCHEMAS = frozenset(
    {
        "bybit_sequence_gap",
        "collector_config",
        "liveness_failure",
        "order_observation",
        "order_request",
        "pre_ack_frame",
        "raw_frame",
        "raw_quarantine",
        "subscription_ack",
        "subscription_send",
        "venue_down",
        "venue_recovered",
    }
)
FROZEN_PAYLOAD_SCHEMAS = LEGACY_PAYLOAD_SCHEMAS | {
    "account_ledger_entry",
    "reconciliation_decision",
    "reconciliation_surface",
    "writer_lease_decision",
    "writer_authority_promotion",
}
ROOT = Path(__file__).parents[1]


def _emitted_payload_schemas() -> set[str]:
    emitted = set()
    for relative in ("data/session.py", "data/collector.py"):
        tree = ast.parse((ROOT / relative).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr in {"_emit", "_ops"}:
                assert len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                assert isinstance(node.args[1].value, str)
                emitted.add(node.args[1].value)
            elif node.func.attr == "_append":
                for keyword in node.keywords:
                    if keyword.arg == "schema":
                        assert isinstance(keyword.value, ast.Constant)
                        assert isinstance(keyword.value.value, str)
                        emitted.add(keyword.value.value)
    return emitted


def market_event() -> dict:
    return {
        "schema_ver": 1,
        "event_kind": "market",
        "payload_schema": "raw_frame",
        "venue": "hyperliquid",
        "conn_id": "conn-1",
        "boot_id": "boot-1",
        "recv_wall_ns": 1_000,
        "recv_mono_ns": 900,
        "source": "live_public_ws",
        "payload": {"raw": "{}"},
    }


def test_valid_envelope_is_retained() -> None:
    event = market_event()
    assert validate_envelope(event) is event


def test_missing_common_field_is_rejected() -> None:
    event = market_event()
    del event["boot_id"]
    with pytest.raises(ContractError, match="boot_id"):
        validate_envelope(event)


def test_event_kind_set_is_frozen() -> None:
    event = market_event()
    event["event_kind"] = "fault"
    with pytest.raises(ContractError, match="event_kind"):
        validate_envelope(event)


def test_payload_schema_registry_is_frozen_complete_and_bounded() -> None:
    emitted = {
        "bybit_sequence_gap",
        "collector_config",
        "liveness_failure",
        "pre_ack_frame",
        "raw_frame",
        "raw_quarantine",
        "subscription_ack",
        "subscription_send",
        "venue_down",
        "venue_recovered",
    }
    assert PAYLOAD_SCHEMAS == FROZEN_PAYLOAD_SCHEMAS
    assert _emitted_payload_schemas() == emitted
    assert emitted <= PAYLOAD_SCHEMAS
    assert len(PAYLOAD_SCHEMAS) <= 20


def test_legacy_schema_v1_envelopes_remain_replay_compatible() -> None:
    for schema in LEGACY_PAYLOAD_SCHEMAS:
        event = market_event()
        event["payload_schema"] = schema
        if schema in {"order_request", "order_observation"}:
            event.update(
                event_kind="order",
                identity_status="known",
                client_order_id="0xlegacy",
                venue_order_id=None,
            )
        if schema == "raw_quarantine":
            event.update(event_kind="ops", payload={"raw": "legacy"})
        assert validate_envelope(event) is event


def _reconciliation_event(schema: str, payload: dict) -> dict:
    event = market_event()
    event.update(
        event_kind="reconciliation", payload_schema=schema, seq_within_boot=7, payload=payload
    )
    return event


def _writer_event(**changes) -> dict:
    payload = {
        "action": "acquire", "outcome": "pending_reconciliation",
        "reason": "lease_acquired", "account_digest": "a" * 64,
        "instance_id": "writer-one", "wallet_fingerprint": "b" * 64,
        "boot_id": "identity-boot", "lease_epoch": 1,
        "lock_path_digest": "c" * 64, "prior_epoch_valid": False,
    }
    payload.update(changes)
    event = market_event()
    event.update(
        event_kind="decision", payload_schema="writer_lease_decision",
        seq_within_boot=8, payload=payload,
    )
    return event


def _bound_order_request(**changes) -> dict:
    payload = {
        "account_digest": "a" * 64,
        "lease_epoch": 1,
        "writer_instance_id": "writer-one",
        "wallet_fingerprint": "b" * 64,
    }
    payload.update(changes)
    event = market_event()
    event.update(
        event_kind="order", payload_schema="order_request", seq_within_boot=9,
        identity_status="known", client_order_id="0xrequest", venue_order_id=None,
        payload=payload,
    )
    return event


@pytest.mark.parametrize(
    ("action", "outcome", "reason", "lease_epoch"),
    [
        ("acquire", "pending_reconciliation", "lease_acquired", 1),
        ("deny", "cancel_only", "incumbent_other_wallet", 4),
        ("deny", "terminated", "shared_writer_identity", 4),
        ("deny", "terminated", "unknown_incumbent", None),
        ("release", "released", "lease_released", 4),
        ("revalidate", "invalidated", "lock_inode_changed", 4),
    ],
)
def test_writer_lease_decision_has_a_closed_action_matrix(
    action: str, outcome: str, reason: str, lease_epoch: int | None
) -> None:
    event = _writer_event(
        action=action, outcome=outcome, reason=reason, lease_epoch=lease_epoch
    )
    assert validate_envelope(event) is event


def test_writer_lease_decision_rejects_ambiguous_or_identifying_payloads() -> None:
    with pytest.raises(ContractError, match="writer decision combination"):
        validate_envelope(_writer_event(action="release", reason="lease_released"))
    with pytest.raises(ContractError, match="account_digest"):
        validate_envelope(_writer_event(account_digest="raw-account-id"))
    with pytest.raises(ContractError, match="fields"):
        validate_envelope(_writer_event(account_id="raw-account-id"))
    event = _writer_event()
    event["event_kind"] = "ops"
    with pytest.raises(ContractError, match="event kind"):
        validate_envelope(event)
    event["event_kind"] = "decision"
    del event["seq_within_boot"]
    with pytest.raises(ContractError, match="seq_within_boot"):
        validate_envelope(event)


def test_versioned_reconciliation_payloads_are_structurally_validated() -> None:
    canonical = {
        "scheme_id": "balances.state",
        "scheme_version": 1,
        "fingerprints": ["sha256:a"],
    }
    surface = {
        "venue": "hyperliquid", "surface": "balances", "observed_ns": 900,
        "fetched_count": 1, "page_complete": True, "truncated": False,
        "unknown_count": 0, "mismatch_count": 0, "entities": canonical,
        "identities": {**canonical, "scheme_id": "balances.identity"},
        "query_window": {"start_ns": 100, "end_ns": 900},
    }
    ledger = {
        "venue": "hyperliquid", "entry_id": "funding-1", "entry_kind": "funding",
        "occurred_ns": 800, "asset": "USDC", "signed_amount_canonical": "-0.125",
        "caused_by_order_id": None, "source_observed_ns": 900,
    }
    decision = {
        "startup_started_ns": 950, "action": "cancel_only_freeze",
        "reasons": ["hyperliquid.balances:unknown_entry"], "input_digest": "a" * 64,
        "window": {"start_ns": 100, "end_ns": 900},
        "schema_versions": {"account_ledger_entry": 1},
    }
    for schema, payload in (
        ("reconciliation_surface", surface),
        ("account_ledger_entry", ledger),
        ("reconciliation_decision", decision),
    ):
        assert validate_envelope(_reconciliation_event(schema, payload))["payload"] == payload
    ledger["entry_kind"] = "fill"
    with pytest.raises(ContractError, match="entry_kind"):
        validate_envelope(_reconciliation_event("account_ledger_entry", ledger))
    ledger.update(entry_kind="funding", signed_amount_canonical="1.00")
    with pytest.raises(ContractError, match="canonical amount"):
        validate_envelope(_reconciliation_event("account_ledger_entry", ledger))


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        ("account_ledger_entry", {"entry_id": "incomplete"}),
        ("reconciliation_decision", {"action": "ready"}),
        ("reconciliation_surface", {"surface": "balances"}),
        ("writer_lease_decision", {"action": "acquire"}),
        ("writer_authority_promotion", {"outcome": "promoted"}),
    ],
)
def test_incomplete_reconciliation_payloads_are_rejected(schema: str, payload: dict) -> None:
    with pytest.raises(ContractError):
        validate_envelope(_reconciliation_event(schema, payload))


def test_unknown_payload_schema_is_rejected() -> None:
    event = market_event()
    event["payload_schema"] = "unregistered_type"
    with pytest.raises(ContractError, match="payload_schema"):
        validate_envelope(event)


def test_order_request_requires_client_identity() -> None:
    event = market_event()
    event.update(
        event_kind="order",
        payload_schema="order_request",
        identity_status="unknown",
        client_order_id=None,
        venue_order_id=None,
    )
    with pytest.raises(ContractError, match="client_order_id"):
        validate_envelope(event)


def test_order_request_lease_binding_is_atomic_and_structurally_valid() -> None:
    assert validate_envelope(_bound_order_request())["seq_within_boot"] == 9

    invalid = [
        _bound_order_request(account_digest="raw-account"),
        _bound_order_request(lease_epoch=0),
        _bound_order_request(writer_instance_id=""),
        _bound_order_request(wallet_fingerprint="b" * 32),
    ]
    seq_only = _bound_order_request()
    seq_only["payload"] = {}
    invalid.append(seq_only)
    no_seq = _bound_order_request()
    no_seq.pop("seq_within_boot")
    invalid.append(no_seq)
    partial = _bound_order_request()
    partial["payload"].pop("wallet_fingerprint")
    invalid.append(partial)
    for event in invalid:
        with pytest.raises(ContractError, match="lease binding"):
            validate_envelope(event)


def test_unknown_order_observation_is_not_discarded() -> None:
    event = market_event()
    event.update(
        event_kind="order",
        payload_schema="order_observation",
        identity_status="unknown",
        client_order_id=None,
        venue_order_id=None,
    )
    assert validate_envelope(event)["identity_status"] == "unknown"


def test_raw_quarantine_requires_original_frame() -> None:
    event = market_event()
    event.update(event_kind="ops", payload_schema="raw_quarantine", payload={})
    with pytest.raises(ContractError, match="raw"):
        validate_envelope(event)


def test_bybit_delta_update_id_detects_discontinuity_but_snapshot_resets() -> None:
    assert not bybit_update_gap(previous_u=40, current_u=41, message_type="delta")
    assert bybit_update_gap(previous_u=40, current_u=43, message_type="delta")
    assert not bybit_update_gap(previous_u=40, current_u=1, message_type="snapshot")


def test_hyperliquid_arrival_interval_is_a_structured_soft_alert() -> None:
    if hasattr(contracts, "hl_arrival_interval_alert"):
        alert = contracts.hl_arrival_interval_alert(
            stream="l2Book:BTC",
            previous_ns=100,
            current_ns=201,
            soft_threshold_ns=100,
        )
    else:
        alert = contracts.hl_interval_gap(previous_ns=100, current_ns=201, max_interval_ns=100)

    assert type(alert) is not bool
    assert alert.stream == "l2Book:BTC"
    assert alert.observed_ns == 101
    assert alert.soft_threshold_ns == 100
    assert alert.exceeded is True
    assert alert.severity == "soft"
    assert not hasattr(contracts, "hl_interval_gap")


def test_monotonic_elapsed_time_never_crosses_boots() -> None:
    previous = market_event()
    current = market_event()
    current["recv_mono_ns"] = 1_000
    assert monotonic_elapsed_ns(previous, current) == 100
    current["boot_id"] = "boot-2"
    with pytest.raises(ContractError, match="boot_id"):
        monotonic_elapsed_ns(previous, current)


def test_manifest_structure_and_checksum_are_fail_closed() -> None:
    payload = b"raw frame\n"
    manifest = {
        "schema_ver": 1,
        "files": [{"path": "part-000.gz", "sha256": "0" * 64, "bytes": 10}],
    }
    checked = deepcopy(manifest)
    assert validate_manifest(checked) is checked
    assert not checksum_matches(payload, manifest["files"][0]["sha256"])
    manifest["files"][0].pop("sha256")
    with pytest.raises(ContractError, match="sha256"):
        validate_manifest(manifest)
