ALTER TABLE policy_decisions ADD COLUMN policy_mode TEXT NOT NULL DEFAULT 'legacy_diagnostic'
    CHECK(policy_mode IN ('deterministic', 'research_randomized', 'legacy_diagnostic'));
ALTER TABLE policy_decisions ADD COLUMN session_id TEXT NOT NULL DEFAULT 'legacy-unknown';
ALTER TABLE policy_decisions ADD COLUMN context_schema_version TEXT NOT NULL DEFAULT 'legacy-unknown';
ALTER TABLE policy_decisions ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE policy_decisions ADD COLUMN context_sha256 TEXT NOT NULL DEFAULT '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'
    CHECK(length(context_sha256) = 64);
ALTER TABLE policy_decisions ADD COLUMN availability_reason TEXT NOT NULL DEFAULT 'legacy-unknown';
ALTER TABLE policy_decisions ADD COLUMN feasible_arms_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE policy_decisions ADD COLUMN propensities_json TEXT;
ALTER TABLE policy_decisions ADD COLUMN supports_ope INTEGER NOT NULL DEFAULT 0
    CHECK(supports_ope IN (0, 1));
ALTER TABLE policy_decisions ADD COLUMN randomization_id TEXT;
ALTER TABLE policy_decisions ADD COLUMN random_seed_hex TEXT;
ALTER TABLE policy_decisions ADD COLUMN random_counter INTEGER
    CHECK(random_counter IS NULL OR random_counter >= 0);
ALTER TABLE policy_decisions ADD COLUMN research_study_id TEXT;
ALTER TABLE policy_decisions ADD COLUMN research_study_epoch TEXT;
ALTER TABLE policy_decisions ADD COLUMN research_consent_version TEXT;
ALTER TABLE policy_decisions ADD COLUMN research_specification_sha256 TEXT
    CHECK(research_specification_sha256 IS NULL OR length(research_specification_sha256) = 64);
ALTER TABLE policy_decisions ADD COLUMN reward_version TEXT NOT NULL DEFAULT 'legacy-v0';

CREATE INDEX IF NOT EXISTS policy_decisions_mode_time_idx
    ON policy_decisions(policy_mode, occurred_at_unix_ms, decision_id);

CREATE TABLE policy_outcome_windows (
    decision_id TEXT NOT NULL REFERENCES policy_decisions(decision_id) ON DELETE CASCADE,
    reward_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'finalized', 'censored')),
    opened_at_unix_ms INTEGER NOT NULL CHECK(opened_at_unix_ms >= 0),
    scheduled_close_at_unix_ms INTEGER NOT NULL
        CHECK(scheduled_close_at_unix_ms >= opened_at_unix_ms),
    finalized_at_unix_ms INTEGER CHECK(
        finalized_at_unix_ms IS NULL OR finalized_at_unix_ms >= opened_at_unix_ms
    ),
    initial_snapshot_json TEXT NOT NULL,
    initial_snapshot_sha256 TEXT NOT NULL CHECK(length(initial_snapshot_sha256) = 64),
    final_snapshot_json TEXT,
    final_snapshot_sha256 TEXT CHECK(
        final_snapshot_sha256 IS NULL OR length(final_snapshot_sha256) = 64
    ),
    contamination_json TEXT NOT NULL DEFAULT '[]',
    missingness_reason TEXT,
    PRIMARY KEY(decision_id, reward_version)
) STRICT;

CREATE INDEX policy_outcome_windows_due_idx
    ON policy_outcome_windows(status, scheduled_close_at_unix_ms);

CREATE TABLE policy_observations (
    observation_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES policy_decisions(decision_id) ON DELETE CASCADE,
    reward_version TEXT NOT NULL,
    observation_kind TEXT NOT NULL CHECK(observation_kind IN (
        'user_rating', 'user_action', 'undo', 'restore_failure'
    )),
    idempotency_key TEXT NOT NULL,
    observed_at_unix_ms INTEGER NOT NULL CHECK(observed_at_unix_ms >= 0),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    UNIQUE(decision_id, reward_version, idempotency_key)
) STRICT;

CREATE INDEX policy_observations_decision_idx
    ON policy_observations(decision_id, reward_version, observed_at_unix_ms, observation_id);

ALTER TABLE policy_rewards RENAME TO policy_rewards_v1;

CREATE TABLE policy_rewards (
    decision_id TEXT NOT NULL REFERENCES policy_decisions(decision_id) ON DELETE CASCADE,
    reward_version TEXT NOT NULL,
    reward_value REAL NOT NULL CHECK(reward_value >= -1.0 AND reward_value <= 1.0),
    finalized_at_unix_ms INTEGER NOT NULL CHECK(finalized_at_unix_ms >= 0),
    components_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    PRIMARY KEY(decision_id, reward_version)
) STRICT;

INSERT INTO policy_rewards(
    decision_id, reward_version, reward_value, finalized_at_unix_ms,
    components_json, payload_sha256
)
SELECT decision_id, reward_version, reward_value, finalized_at_unix_ms,
       components_json, payload_sha256
FROM policy_rewards_v1;

DROP TABLE policy_rewards_v1;

CREATE TABLE research_exports (
    export_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL,
    study_epoch TEXT NOT NULL,
    specification_sha256 TEXT NOT NULL CHECK(length(specification_sha256) = 64),
    dataset_sha256 TEXT NOT NULL CHECK(length(dataset_sha256) = 64),
    filename TEXT NOT NULL UNIQUE,
    row_count INTEGER NOT NULL CHECK(row_count >= 0),
    created_at_unix_ms INTEGER NOT NULL CHECK(created_at_unix_ms >= 0)
) STRICT;
