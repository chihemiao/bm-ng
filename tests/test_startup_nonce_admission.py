import pytest

from reconciliation.ledger import BalanceLedger
from reconciliation.state import (
    AdmissionDecision,
    CanonicalSet,
    ExpectedSurface,
    SurfaceEvidence,
    VenueEvidence,
    VenueExpectation,
    decide_startup_admission,
)

FINGERPRINT = "a" * 64
OTHER_FINGERPRINT = "b" * 64


def _surface() -> SurfaceEvidence:
    return SurfaceEvidence(
        observed_ns=150,
        fetched_count=1,
        page_complete=True,
        truncated=False,
        unknown_count=0,
        mismatch_count=0,
        entities=CanonicalSet("state", 1, frozenset({"state"})),
        identities=CanonicalSet("identity", 1, frozenset({"identity"})),
    )


def _startup_arguments() -> dict:
    surface = _surface()
    surfaces = {name: surface for name in ("orders", "fills", "positions", "balances")}
    venue = VenueEvidence(**surfaces)
    expected = ExpectedSurface(surface.entities, surface.identities)
    ledger = BalanceLedger(0, 150, (("USDC", "1"),), (("USDC", "1"),), (), frozenset(), True)
    expectation = VenueExpectation(
        **{name: expected for name in ("orders", "fills", "positions", "balances")},
        frozen_intents=frozenset(),
        balance_ledger=ledger,
    )
    return {
        "startup_started_ns": 100,
        "now_ns": 200,
        "venues": {"hyperliquid": venue, "bybit": venue},
        "expectations": {"hyperliquid": expectation, "bybit": expectation},
    }


def _event(
    *,
    fingerprint: str = FINGERPRINT,
    outcome: str = "frozen",
    reason: str = "clock_backward",
    allocated_nonce=None,
    previous_nonce: int = 10,
) -> dict:
    return {
        "payload_schema": "signer_nonce_allocation",
        "payload": {
            "wallet_fingerprint": fingerprint,
            "outcome": outcome,
            "reason": reason,
            "allocated_nonce": allocated_nonce,
            "previous_nonce": previous_nonce,
        },
    }


def _allocated(allocated_nonce: int, previous_nonce: int, **changes) -> dict:
    return _event(
        outcome="allocated",
        reason="nonce_allocated",
        allocated_nonce=allocated_nonce,
        previous_nonce=previous_nonce,
        **changes,
    )


def _decision(events=(), **changes) -> AdmissionDecision:
    arguments = _startup_arguments()
    arguments.update(
        signer_nonce_events=events,
        signer_wallet_fingerprint=FINGERPRINT,
    )
    arguments.update(changes)
    return decide_startup_admission(**arguments)


def test_clean_signer_nonce_stream_is_ready() -> None:
    assert _decision() == AdmissionDecision("ready", ())


@pytest.mark.parametrize("reason", ["clock_backward", "fence_invalidated"])
def test_persisted_signer_nonce_freeze_blocks_startup(reason: str) -> None:
    decision = _decision([_event(reason=reason)])

    assert decision == AdmissionDecision(
        "cancel_only_freeze", (f"signer_nonce_allocation:frozen:{reason}",)
    )


def test_signer_nonce_chain_break_blocks_startup_with_exact_reason() -> None:
    decision = _decision([_allocated(11, 10), _allocated(13, 12)])

    assert decision.reasons == ("signer_nonce_conflict:chain_break:11:12",)


def test_allocation_after_freeze_reports_both_nonce_reason_families() -> None:
    events = [_allocated(11, 10), _event(previous_nonce=11), _allocated(12, 11)]

    assert _decision(events).reasons == (
        "signer_nonce_allocation:frozen:clock_backward",
        "signer_nonce_conflict:allocation_after_freeze:clock_backward",
    )


def test_other_signer_rows_do_not_block_startup() -> None:
    assert _decision([_event(fingerprint=OTHER_FINGERPRINT)]).action == "ready"


def test_previous_and_nonce_freezes_are_both_reported() -> None:
    previous = AdmissionDecision("cancel_only_freeze", ("earlier",))

    assert _decision([_event()], previous_freeze=previous).reasons == (
        "signer_nonce_allocation:frozen:clock_backward",
        "startup:previous_freeze",
    )


def test_malformed_matching_nonce_row_propagates_value_error() -> None:
    malformed = {"payload_schema": "signer_nonce_allocation", "payload": None}

    with pytest.raises(ValueError, match="payload"):
        _decision([malformed])


def test_generator_and_tuple_nonce_streams_have_the_same_result() -> None:
    events = (_allocated(11, 10), _allocated(13, 12))

    assert _decision((event for event in events)) == _decision(events)


@pytest.mark.parametrize(
    "provided",
    [{"signer_nonce_events": ()}, {"signer_wallet_fingerprint": FINGERPRINT}],
)
def test_both_signer_nonce_arguments_are_required(provided: dict) -> None:
    with pytest.raises(TypeError):
        decide_startup_admission(**_startup_arguments(), **provided)


def test_duplicate_frozen_rows_propagate_value_error() -> None:
    with pytest.raises(ValueError, match="multiple signer nonce freeze rows"):
        _decision([_event(), _event(reason="fence_invalidated")])
