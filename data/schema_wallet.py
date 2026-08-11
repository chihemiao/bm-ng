from collections.abc import Mapping

ASSESSMENTS = frozenset({"active", "rotation_due", "expired"})
NON_ACTIVE = frozenset({"rotation_due", "expired"})
ROTATION_COMBINATIONS = {
    ("rotated", "rotation_completed"): NON_ACTIVE,
    ("aborted", "not_due"): frozenset({"active"}),
    ("aborted", "same_wallet"): ASSESSMENTS,
    ("aborted", "identity_changed"): ASSESSMENTS,
    ("aborted", "release_failed"): NON_ACTIVE,
    ("aborted", "acquire_failed"): NON_ACTIVE,
}


def wallet_rotation_semantic_errors(
    payload: Mapping[str, object],
    *,
    validity_ns: int,
    rotation_lead_ns: int,
) -> tuple[str, ...]:
    """Return every lifecycle contradiction in an already format-checked payload."""
    errors: list[str] = []
    old_issued = payload["old_issued_ns"]
    old_expires = payload["old_expires_ns"]
    new_issued = payload["new_issued_ns"]
    new_expires = payload["new_expires_ns"]
    decided = payload["decided_ns"]
    if old_expires != old_issued + validity_ns:
        errors.append("invalid old wallet validity")
    if new_expires != new_issued + validity_ns:
        errors.append("invalid new wallet validity")
    if decided < old_issued:
        errors.append("wallet rotation decision predates old wallet issuance")
    if decided >= old_expires:
        expected_assessment = "expired"
    elif decided >= old_expires - rotation_lead_ns:
        expected_assessment = "rotation_due"
    else:
        expected_assessment = "active"
    assessment = payload["assessment"]
    if assessment != expected_assessment:
        errors.append("wallet assessment contradicts decided time")
    fingerprints_equal = (
        payload["old_wallet_fingerprint"] == payload["new_wallet_fingerprint"]
    )
    if fingerprints_equal != (payload["reason"] == "same_wallet"):
        errors.append("invalid wallet fingerprint relation")
    allowed = ROTATION_COMBINATIONS.get((payload["outcome"], payload["reason"]), frozenset())
    if assessment not in allowed:
        errors.append("invalid wallet rotation combination")
    return tuple(errors)
