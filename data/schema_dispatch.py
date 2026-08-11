"""Static schema registries and closed decision matrices."""

VALIDITY_NS = 30 * 86_400 * 1_000_000_000
ROTATION_LEAD_NS = 7 * 86_400 * 1_000_000_000

EVENT_KINDS = frozenset({"market", "decision", "order", "reconciliation", "ops"})
IDENTITY_STATUSES = frozenset({"known", "unknown"})
ORDER_LEASE_FIELDS = (
    "account_digest", "lease_epoch", "writer_instance_id", "wallet_fingerprint",
)
ORDER_SIGNER_FIELDS = ("allocated_nonce",)
ORDER_BOUND_FIELDS = ORDER_LEASE_FIELDS + ORDER_SIGNER_FIELDS
SURFACES = frozenset({"orders", "fills", "positions", "balances"})
LEDGER_KINDS = frozenset({"funding", "fee", "transfer", "adjustment"})
COMMON_FIELDS = (
    "schema_ver",
    "event_kind",
    "payload_schema",
    "venue",
    "conn_id",
    "boot_id",
    "recv_wall_ns",
    "recv_mono_ns",
    "source",
    "payload",
)
PAYLOAD_SCHEMAS = frozenset(
    {
        "agent_wallet_rotation",
        "bybit_sequence_gap",
        "account_ledger_entry",
        "collector_config",
        "liveness_failure",
        "order_observation",
        "order_request",
        "pre_ack_frame",
        "raw_frame",
        "raw_quarantine",
        "reconciliation_decision",
        "reconciliation_surface",
        "signer_nonce_allocation",
        "subscription_ack",
        "subscription_send",
        "venue_down",
        "venue_recovered",
        "writer_authority_promotion",
        "writer_lease_decision",
    }
)
RECONCILIATION_SCHEMAS = frozenset(
    {"account_ledger_entry", "reconciliation_decision", "reconciliation_surface"}
)
DURABLE_EVENT_SCHEMAS = RECONCILIATION_SCHEMAS | {
    "agent_wallet_rotation", "order_request", "writer_authority_promotion",
    "signer_nonce_allocation", "writer_lease_decision",
}
AUTHORIZATION_DENIAL_REASONS = frozenset(
    {
        "authorize_denied:pending_reconciliation:submit:action_not_authorized",
        "authorize_denied:pending_reconciliation:reduce_only:action_not_authorized",
        "authorize_denied:pending_reconciliation:close:action_not_authorized",
        "authorize_denied:pending_reconciliation:market:action_not_authorized",
        "authorize_denied:pending_reconciliation:modify:native_modify_disabled",
        "authorize_denied:cancel_only:submit:action_not_authorized",
        "authorize_denied:cancel_only:reduce_only:action_not_authorized",
        "authorize_denied:cancel_only:close:action_not_authorized",
        "authorize_denied:cancel_only:market:action_not_authorized",
        "authorize_denied:cancel_only:modify:native_modify_disabled",
        "authorize_denied:risk_increasing:modify:native_modify_disabled",
    }
)
WRITER_DECISIONS = {
    ("acquire", "pending_reconciliation"): frozenset({"lease_acquired"}),
    ("deny", "cancel_only"): frozenset({"incumbent_other_wallet"}),
    ("deny", "terminated"): frozenset(
        {"shared_writer_identity", "unknown_incumbent", "unsafe_lock_file"}
    ),
    ("release", "released"): frozenset({"lease_released"}),
    ("revalidate", "invalidated"): frozenset({"lock_inode_changed"}),
    ("authorize", "denied"): AUTHORIZATION_DENIAL_REASONS,
}
PROMOTION_DECISIONS = {
    ("promoted", "pending_reconciliation", "risk_increasing", "admission_ready"):
        frozenset({"ready"}),
    ("denied", "pending_reconciliation", "pending_reconciliation", "admission_freeze"):
        frozenset({"cancel_only_freeze"}),
    ("denied", "cancel_only", "cancel_only", "not_promotable_mode"):
        frozenset({"ready", "cancel_only_freeze"}),
}
