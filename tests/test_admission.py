from decimal import Decimal

import pytest

from reconciliation.admission import (
    CONTINUOUS_ADMISSION_REASON_KEYS,
    decide_continuous_admission,
)
from reconciliation.clock import StateClock
from reconciliation.exposure import ExposureClock
from reconciliation.fx import Notional
from reconciliation.legs import PairState
from reconciliation.promotion import demotion_reason
from reconciliation.state import AdmissionDecision, StartupContractError


@pytest.mark.parametrize(
    ("action", "reasons"),
    [
        ("unknown", ()),
        ("ready", ("looks-fine",)),
        ("cancel_only_freeze", ()),
        ("cancel_only_freeze", ("second", "first")),
        ("cancel_only_freeze", ("same", "same")),
        ("cancel_only_freeze", ("",)),
        ("cancel_only_freeze", ["not-a-tuple"]),
    ],
)
def test_admission_decision_rejects_inconsistent_direct_construction(
    action: object,
    reasons: object,
) -> None:
    with pytest.raises(StartupContractError):
        AdmissionDecision(action, reasons)  # type: ignore[arg-type]


def test_admission_decision_accepts_only_canonical_ready_and_freeze() -> None:
    assert AdmissionDecision("ready", ()).reasons == ()
    assert AdmissionDecision("cancel_only_freeze", ("reason",)).reasons == ("reason",)


def _continuous(**changes) -> AdmissionDecision:
    values = {
        "exposure": ExposureClock("flat", 100, None, False),
        "obligation": StateClock("inactive", 100, None, False),
        "pair": PairState("balanced", ()),
        "agent_wallet_status": "active",
        "nonce_freeze_reason": None,
        "naked_notional": Notional(Decimal(0), "USDC"),
        "max_naked_notional": Notional(Decimal("1000"), "USDC"),
    }
    values.update(changes)
    return decide_continuous_admission(**values)


def test_clear_continuous_risk_state_is_ready_without_reasons() -> None:
    assert _continuous() == AdmissionDecision("ready", ())


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"exposure": None}, "continuous_admission:exposure_unobserved"),
        (
            {"exposure": ExposureClock("unknown", 100, None, False)},
            "continuous_admission:exposure_unknown",
        ),
        (
            {"exposure": ExposureClock("naked", 111, 100, True)},
            "continuous_admission:naked_duration_exceeded",
        ),
        (
            {"exposure": ExposureClock("flat", 100, None, None)},
            "continuous_admission:naked_duration_unknown",
        ),
        ({"obligation": None}, "continuous_admission:obligation_unobserved"),
        (
            {"obligation": StateClock("active", 111, 100, True)},
            "continuous_admission:obligation_duration_exceeded",
        ),
        (
            {"obligation": StateClock("inactive", 100, None, None)},
            "continuous_admission:obligation_duration_unknown",
        ),
        (
            {"pair": PairState("unknown", (("bybit", "unknown"),))},
            "continuous_admission:pair_unknown",
        ),
        (
            {"agent_wallet_status": "expired"},
            "continuous_admission:agent_wallet_expired",
        ),
        (
            {"nonce_freeze_reason": "signer_nonce:conflict"},
            "continuous_admission:nonce_frozen:signer_nonce:conflict",
        ),
    ],
)
def test_each_continuous_risk_condition_freezes_with_its_exact_reason(changes, reason):
    assert _continuous(**changes) == AdmissionDecision("cancel_only_freeze", (reason,))


@pytest.mark.parametrize(
    "changes",
    [
        {"exposure": ExposureClock("naked", 105, 100, False)},
        {
            "obligation": StateClock("active", 105, 100, False),
            "pair": PairState("imbalanced", (("bybit", "partial"),)),
        },
        {"agent_wallet_status": "rotation_due"},
    ],
)
def test_expected_in_flight_or_rotation_due_state_does_not_freeze(changes) -> None:
    assert _continuous(**changes) == AdmissionDecision("ready", ())


def test_all_continuous_freeze_reasons_accumulate_canonically() -> None:
    decision = _continuous(
        exposure=None,
        obligation=None,
        pair=PairState("unknown", (("hyperliquid", "unknown"),)),
        agent_wallet_status="expired",
        nonce_freeze_reason="nonce-conflict",
    )
    assert decision.reasons == tuple(
        sorted(
            {
                "continuous_admission:exposure_unobserved",
                "continuous_admission:obligation_unobserved",
                "continuous_admission:pair_unknown",
                "continuous_admission:agent_wallet_expired",
                "continuous_admission:nonce_frozen:nonce-conflict",
            }
        )
    )


@pytest.mark.parametrize(
    ("amount", "maximum", "action", "reasons"),
    [
        ("999", "1000", "ready", ()),
        ("1000", "1000", "ready", ()),
        ("1001", "1000", "cancel_only_freeze", ("continuous_admission:notional_exceeded",)),
        ("0", "0", "ready", ()),
        ("0.01", "0", "cancel_only_freeze", ("continuous_admission:notional_exceeded",)),
    ],
)
def test_notional_limit_has_an_inclusive_safe_boundary(amount, maximum, action, reasons):
    decision = _continuous(
        naked_notional=Notional(Decimal(amount), "USDC"),
        max_naked_notional=Notional(Decimal(maximum), "USDC"),
    )
    assert decision == AdmissionDecision(action, reasons)


def test_unknown_notional_freezes_with_exact_reason() -> None:
    assert _continuous(naked_notional=None) == AdmissionDecision(
        "cancel_only_freeze", ("continuous_admission:notional_unknown",)
    )


def test_notional_and_limit_quotes_must_match() -> None:
    with pytest.raises(ValueError, match="quote"):
        _continuous(naked_notional=Notional(Decimal("1"), "USDT"))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"naked_notional": Decimal("1")}, "naked_notional"),
        ({"max_naked_notional": Decimal("1")}, "max_naked_notional"),
        ({"max_naked_notional": None}, "max_naked_notional"),
    ],
)
def test_continuous_admission_requires_typed_notionals(changes, message) -> None:
    with pytest.raises(TypeError, match=message):
        _continuous(**changes)


def test_notional_reason_accumulates_with_existing_freeze_reasons() -> None:
    decision = _continuous(
        exposure=None,
        naked_notional=Notional(Decimal("1001"), "USDC"),
    )
    assert decision.reasons == (
        "continuous_admission:exposure_unobserved",
        "continuous_admission:notional_exceeded",
    )


def test_continuous_reason_key_set_has_twelve_members() -> None:
    assert len(CONTINUOUS_ADMISSION_REASON_KEYS) == 12


@pytest.mark.parametrize(
    "reason",
    [
        "continuous_admission:notional_exceeded",
        "continuous_admission:notional_unknown",
    ],
)
def test_notional_reason_keys_are_valid_demotion_evidence(reason: str) -> None:
    assert reason in CONTINUOUS_ADMISSION_REASON_KEYS
    admission = AdmissionDecision("cancel_only_freeze", (reason,))
    assert demotion_reason(admission) == f"writer_demoted:{reason}"
