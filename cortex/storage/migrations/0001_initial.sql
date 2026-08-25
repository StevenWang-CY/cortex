CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    applied_at_unix_ms INTEGER NOT NULL CHECK(applied_at_unix_ms >= 0)
) STRICT;

CREATE TABLE IF NOT EXISTS legacy_migrations (
    migration_id INTEGER PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    backup_name TEXT NOT NULL,
    backup_sha256 TEXT NOT NULL CHECK(length(backup_sha256) = 64),
    imported_records INTEGER NOT NULL CHECK(imported_records >= 0),
    skipped_records INTEGER NOT NULL DEFAULT 0 CHECK(skipped_records >= 0),
    status TEXT NOT NULL DEFAULT 'imported'
        CHECK(status IN ('imported', 'skipped')),
    diagnostic_code TEXT,
    imported_at_unix_ms INTEGER NOT NULL CHECK(imported_at_unix_ms >= 0),
    CHECK(
        (status = 'imported' AND diagnostic_code IS NULL)
        OR (status = 'skipped' AND diagnostic_code IS NOT NULL)
    ),
    UNIQUE(source_kind, source_sha256)
) STRICT;

CREATE TABLE IF NOT EXISTS key_values (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value_kind TEXT NOT NULL CHECK(value_kind IN ('json', 'integer', 'float')),
    value_json TEXT NOT NULL,
    value_sha256 TEXT NOT NULL CHECK(length(value_sha256) = 64),
    expires_at_unix_ms INTEGER CHECK(expires_at_unix_ms IS NULL OR expires_at_unix_ms >= 0),
    updated_at_unix_ms INTEGER NOT NULL CHECK(updated_at_unix_ms >= 0),
    PRIMARY KEY(namespace, key)
) STRICT;

CREATE INDEX IF NOT EXISTS key_values_expiry_idx
    ON key_values(expires_at_unix_ms)
    WHERE expires_at_unix_ms IS NOT NULL;

CREATE TABLE IF NOT EXISTS intervention_transactions (
    intervention_id TEXT PRIMARY KEY,
    manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256) = 64),
    lifecycle_state TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    created_at_unix_ms INTEGER NOT NULL CHECK(created_at_unix_ms >= 0),
    updated_at_unix_ms INTEGER NOT NULL CHECK(updated_at_unix_ms >= created_at_unix_ms),
    aggregate_json TEXT NOT NULL,
    aggregate_sha256 TEXT NOT NULL CHECK(length(aggregate_sha256) = 64)
) STRICT;

CREATE INDEX IF NOT EXISTS intervention_transactions_state_idx
    ON intervention_transactions(lifecycle_state, updated_at_unix_ms);

CREATE TABLE IF NOT EXISTS intervention_authorizations (
    authorization_id TEXT PRIMARY KEY,
    intervention_id TEXT NOT NULL REFERENCES intervention_transactions(intervention_id) ON DELETE CASCADE,
    authorization_request_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    issued_at_unix_ms INTEGER NOT NULL CHECK(issued_at_unix_ms >= 0),
    expires_at_unix_ms INTEGER NOT NULL CHECK(expires_at_unix_ms >= issued_at_unix_ms),
    consumed_at_unix_ms INTEGER CHECK(consumed_at_unix_ms IS NULL OR consumed_at_unix_ms >= issued_at_unix_ms),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64)
) STRICT;

CREATE INDEX IF NOT EXISTS intervention_authorizations_transaction_idx
    ON intervention_authorizations(intervention_id, issued_at_unix_ms);

CREATE TABLE IF NOT EXISTS intervention_receipts (
    receipt_id TEXT PRIMARY KEY,
    intervention_id TEXT NOT NULL REFERENCES intervention_transactions(intervention_id) ON DELETE CASCADE,
    command_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('apply', 'compensate', 'restore')),
    attempt INTEGER NOT NULL CHECK(attempt BETWEEN 1 AND 100),
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('succeeded', 'failed', 'already_complete')),
    verification TEXT NOT NULL CHECK(verification IN ('verified', 'failed', 'not_applicable')),
    ended_at_unix_ms INTEGER NOT NULL CHECK(ended_at_unix_ms >= 0),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64)
) STRICT;

CREATE INDEX IF NOT EXISTS intervention_receipts_transaction_idx
    ON intervention_receipts(intervention_id, phase, ended_at_unix_ms);

CREATE TABLE IF NOT EXISTS intervention_transitions (
    intervention_id TEXT NOT NULL REFERENCES intervention_transactions(intervention_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    at_unix_ms INTEGER NOT NULL CHECK(at_unix_ms >= 0),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    PRIMARY KEY(intervention_id, ordinal)
) STRICT;

CREATE TABLE IF NOT EXISTS intervention_dispatch_bindings (
    command_id TEXT PRIMARY KEY,
    intervention_id TEXT NOT NULL REFERENCES intervention_transactions(intervention_id) ON DELETE CASCADE,
    bound_at_unix_ms INTEGER NOT NULL CHECK(bound_at_unix_ms >= 0),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64)
) STRICT;

CREATE TABLE IF NOT EXISTS intervention_restores (
    restore_id TEXT PRIMARY KEY,
    intervention_id TEXT NOT NULL REFERENCES intervention_transactions(intervention_id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    requested_at_unix_ms INTEGER NOT NULL CHECK(requested_at_unix_ms >= 0),
    is_active INTEGER NOT NULL CHECK(is_active IN (0, 1)),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64)
) STRICT;

CREATE INDEX IF NOT EXISTS intervention_restores_transaction_idx
    ON intervention_restores(intervention_id, requested_at_unix_ms);

CREATE TABLE IF NOT EXISTS intervention_consent_evidence (
    intervention_id TEXT NOT NULL REFERENCES intervention_transactions(intervention_id) ON DELETE CASCADE,
    receipt_id TEXT NOT NULL REFERENCES intervention_receipts(receipt_id) ON DELETE CASCADE,
    PRIMARY KEY(intervention_id, receipt_id)
) STRICT;

CREATE TABLE IF NOT EXISTS policy_decisions (
    decision_id TEXT PRIMARY KEY,
    decision_point_id TEXT NOT NULL UNIQUE,
    policy_name TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_state_sha256 TEXT NOT NULL CHECK(length(policy_state_sha256) = 64),
    selected_arm TEXT NOT NULL,
    selected_probability REAL NOT NULL CHECK(selected_probability >= 0.0 AND selected_probability <= 1.0),
    eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
    available INTEGER NOT NULL CHECK(available IN (0, 1)),
    occurred_at_unix_ms INTEGER NOT NULL CHECK(occurred_at_unix_ms >= 0),
    occurred_at_mono_ns INTEGER NOT NULL CHECK(occurred_at_mono_ns >= 0),
    boot_id TEXT NOT NULL,
    intervention_id TEXT,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64)
) STRICT;

CREATE INDEX IF NOT EXISTS policy_decisions_intervention_idx
    ON policy_decisions(intervention_id);

CREATE TABLE IF NOT EXISTS policy_deliveries (
    decision_id TEXT PRIMARY KEY REFERENCES policy_decisions(decision_id) ON DELETE CASCADE,
    delivery_status TEXT NOT NULL,
    delivered_at_unix_ms INTEGER CHECK(delivered_at_unix_ms IS NULL OR delivered_at_unix_ms >= 0),
    intervention_id TEXT,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64)
) STRICT;

CREATE TABLE IF NOT EXISTS policy_outcomes (
    decision_id TEXT PRIMARY KEY REFERENCES policy_decisions(decision_id) ON DELETE CASCADE,
    outcome_status TEXT NOT NULL,
    window_opened_at_unix_ms INTEGER NOT NULL CHECK(window_opened_at_unix_ms >= 0),
    window_closed_at_unix_ms INTEGER NOT NULL CHECK(window_closed_at_unix_ms >= window_opened_at_unix_ms),
    contamination_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64)
) STRICT;

CREATE TABLE IF NOT EXISTS policy_rewards (
    decision_id TEXT PRIMARY KEY REFERENCES policy_decisions(decision_id) ON DELETE CASCADE,
    reward_version TEXT NOT NULL,
    reward_value REAL NOT NULL CHECK(reward_value >= -1.0 AND reward_value <= 1.0),
    finalized_at_unix_ms INTEGER NOT NULL CHECK(finalized_at_unix_ms >= 0),
    components_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64)
) STRICT;

CREATE TABLE IF NOT EXISTS policy_states (
    policy_name TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    state_json TEXT NOT NULL,
    state_sha256 TEXT NOT NULL CHECK(length(state_sha256) = 64),
    updated_at_unix_ms INTEGER NOT NULL CHECK(updated_at_unix_ms >= 0),
    PRIMARY KEY(policy_name, policy_version)
) STRICT;

CREATE TABLE IF NOT EXISTS calibration_profiles (
    profile_id TEXT PRIMARY KEY,
    profile_sha256 TEXT NOT NULL CHECK(length(profile_sha256) = 64),
    provenance TEXT NOT NULL CHECK(provenance IN ('measured', 'demo')),
    approved_at_unix_ms INTEGER CHECK(approved_at_unix_ms IS NULL OR approved_at_unix_ms >= 0),
    created_at_unix_ms INTEGER NOT NULL CHECK(created_at_unix_ms >= 0),
    profile_json TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS active_calibration (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    profile_id TEXT NOT NULL REFERENCES calibration_profiles(profile_id),
    profile_sha256 TEXT NOT NULL CHECK(length(profile_sha256) = 64),
    activated_at_unix_ms INTEGER NOT NULL CHECK(activated_at_unix_ms >= 0)
) STRICT;

CREATE TABLE IF NOT EXISTS session_aggregates (
    session_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
    started_at_unix_ms INTEGER NOT NULL CHECK(started_at_unix_ms >= 0),
    ended_at_unix_ms INTEGER NOT NULL CHECK(ended_at_unix_ms >= started_at_unix_ms),
    duration_seconds REAL NOT NULL CHECK(duration_seconds >= 0.0),
    flow_seconds REAL NOT NULL CHECK(flow_seconds >= 0.0),
    hyper_seconds REAL NOT NULL CHECK(hyper_seconds >= 0.0),
    hypo_seconds REAL NOT NULL CHECK(hypo_seconds >= 0.0),
    recovery_seconds REAL NOT NULL CHECK(recovery_seconds >= 0.0),
    interventions_triggered INTEGER NOT NULL CHECK(interventions_triggered >= 0),
    interventions_accepted INTEGER NOT NULL CHECK(interventions_accepted >= 0),
    breaks_taken INTEGER NOT NULL CHECK(breaks_taken >= 0),
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    expires_at_unix_ms INTEGER CHECK(expires_at_unix_ms IS NULL OR expires_at_unix_ms >= ended_at_unix_ms)
) STRICT;

CREATE INDEX IF NOT EXISTS session_aggregates_end_idx
    ON session_aggregates(ended_at_unix_ms);

CREATE TABLE IF NOT EXISTS analytics_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT,
    occurred_at_unix_ms INTEGER NOT NULL CHECK(occurred_at_unix_ms >= 0),
    occurred_at_mono_ns INTEGER NOT NULL CHECK(occurred_at_mono_ns >= 0),
    boot_id TEXT NOT NULL,
    privacy_class TEXT NOT NULL CHECK(privacy_class IN ('operational', 'derived', 'sensitive_derived')),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    expires_at_unix_ms INTEGER NOT NULL CHECK(expires_at_unix_ms >= occurred_at_unix_ms)
) STRICT;

CREATE INDEX IF NOT EXISTS analytics_events_expiry_idx
    ON analytics_events(expires_at_unix_ms);
