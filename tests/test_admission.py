import pytest

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
    action: object, reasons: object,
) -> None:
    with pytest.raises(StartupContractError):
        AdmissionDecision(action, reasons)  # type: ignore[arg-type]


def test_admission_decision_accepts_only_canonical_ready_and_freeze() -> None:
    assert AdmissionDecision("ready", ()).reasons == ()
    assert AdmissionDecision("cancel_only_freeze", ("reason",)).reasons == ("reason",)
