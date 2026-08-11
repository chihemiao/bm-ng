"""Format and semantic checks for signer nonce allocation evidence."""

from collections.abc import Mapping

# Venue rule (retrieved 2026-08-11):
# https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets
DAY_MS = 86_400_000  # now_ms is the allocator's local pre-submit proxy for block T.
FIELDS = frozenset(
    {
        "wallet_fingerprint",
        "account_digest",
        "instance_id",
        "allocated_nonce",
        "previous_nonce",
        "now_ms",
        "outcome",
        "reason",
        "decided_ns",
    }
)
OUTCOMES = frozenset({"allocated", "frozen"})
REASONS = frozenset(
    {"nonce_allocated", "clock_backward", "fence_invalidated"}
)


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def signer_nonce_allocation_errors(payload: Mapping[str, object]) -> tuple[str, ...]:
    """Return every format or lifecycle contradiction in a nonce decision."""
    if not isinstance(payload, Mapping) or set(payload) != FIELDS:
        return ("invalid signer nonce fields",)
    allocated = payload["allocated_nonce"]
    previous = payload["previous_nonce"]
    now_ms = payload["now_ms"]
    checks = (
        (_digest(payload["wallet_fingerprint"]), "invalid wallet_fingerprint"),
        (_digest(payload["account_digest"]), "invalid account_digest"),
        (isinstance(payload["instance_id"], str) and bool(payload["instance_id"]),
         "invalid instance_id"),
        (allocated is None or _positive_int(allocated), "invalid allocated_nonce"),
        (type(previous) is int and previous >= 0, "invalid previous_nonce"),
        (_positive_int(now_ms), "invalid now_ms"),
        (payload["outcome"] in OUTCOMES, "invalid nonce outcome"),
        (payload["reason"] in REASONS, "invalid nonce reason"),
        (_positive_int(payload["decided_ns"]), "invalid decided_ns"),
    )
    errors = [message for valid, message in checks if not valid]
    if errors:
        return tuple(errors)
    outcome, reason = payload["outcome"], payload["reason"]
    relations = (
        (outcome != "frozen" or allocated is None,
         "allocated_nonce must be null when outcome is frozen"),
        (outcome != "allocated" or allocated is not None,
         "allocated_nonce must be a positive integer when outcome is allocated"),
        (outcome != "allocated" or reason == "nonce_allocated",
         "outcome allocated requires reason nonce_allocated"),
        (reason != "nonce_allocated" or outcome == "allocated",
         "reason nonce_allocated requires outcome allocated"),
    )
    errors.extend(message for valid, message in relations if not valid)
    if outcome == "allocated" and allocated is not None:
        bounds = (
            (allocated > previous, "allocated_nonce must exceed previous_nonce"),
            (allocated > now_ms - 2 * DAY_MS,
             "allocated_nonce must be strictly within now_ms minus two days"),
            (allocated < now_ms + DAY_MS,
             "allocated_nonce must be strictly within now_ms plus one day"),
        )
        errors.extend(message for valid, message in bounds if not valid)
    return tuple(errors)
