from copy import deepcopy

import pytest

from data import contracts
from data.contracts import (
    ContractError,
    bybit_update_gap,
    checksum_matches,
    monotonic_elapsed_ns,
    validate_envelope,
    validate_manifest,
)


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
