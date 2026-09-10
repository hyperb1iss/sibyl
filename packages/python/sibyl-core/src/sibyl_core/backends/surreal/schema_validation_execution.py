"""Private validation execution history, separate from publication success."""

VALIDATION_EXECUTION_SCHEMA = """
DEFINE TABLE IF NOT EXISTS memory_validation_executions SCHEMAFULL;
ALTER TABLE IF EXISTS memory_validation_executions SCHEMAFULL;
ALTER TABLE IF EXISTS memory_validation_executions PERMISSIONS NONE;
DEFINE FIELD IF NOT EXISTS uuid ON memory_validation_executions TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON memory_validation_executions TYPE string;
DEFINE FIELD IF NOT EXISTS principal_id ON memory_validation_executions TYPE string;
DEFINE FIELD IF NOT EXISTS parent_id ON memory_validation_executions TYPE string;
DEFINE FIELD IF NOT EXISTS source_ids ON memory_validation_executions TYPE array<string>;
DEFINE FIELD IF NOT EXISTS request_sha256 ON memory_validation_executions TYPE string;
DEFINE FIELD IF NOT EXISTS request_json ON memory_validation_executions TYPE string;
DEFINE FIELD IF NOT EXISTS policy_json ON memory_validation_executions TYPE string;
DEFINE FIELD IF NOT EXISTS claim_id ON memory_validation_executions TYPE string;
DEFINE FIELD IF NOT EXISTS state ON memory_validation_executions TYPE string
    ASSERT $value IN ['running', 'recorded', 'returned', 'failed', 'cancelled', 'fenced'];
DEFINE FIELD IF NOT EXISTS result_json ON memory_validation_executions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS usage_json ON memory_validation_executions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS error_type ON memory_validation_executions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS purged ON memory_validation_executions TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS created_at ON memory_validation_executions TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS memory_validation_execution_uuid ON memory_validation_executions FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS memory_validation_execution_owner ON memory_validation_executions FIELDS organization_id, principal_id, parent_id;
DEFINE TABLE IF NOT EXISTS memory_validation_attempts SCHEMAFULL;
ALTER TABLE IF EXISTS memory_validation_attempts SCHEMAFULL;
ALTER TABLE IF EXISTS memory_validation_attempts PERMISSIONS NONE;
DEFINE FIELD IF NOT EXISTS uuid ON memory_validation_attempts TYPE string;
DEFINE FIELD IF NOT EXISTS execution_id ON memory_validation_attempts TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON memory_validation_attempts TYPE string;
DEFINE FIELD IF NOT EXISTS principal_id ON memory_validation_attempts TYPE string;
DEFINE FIELD IF NOT EXISTS outcome_json ON memory_validation_attempts TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON memory_validation_attempts TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS memory_validation_attempt_uuid ON memory_validation_attempts FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS memory_validation_attempt_execution ON memory_validation_attempts FIELDS execution_id;
"""

VALIDATION_PURGE_EVENT = """
DEFINE EVENT IF NOT EXISTS memory_validation_purge ON raw_captures WHEN $event = 'DELETE'
THEN {
    UPDATE memory_validation_executions SET result_json = NONE, purged = true
        WHERE organization_id = $before.organization_id
            AND (parent_id = $before.uuid OR $before.uuid IN source_ids);
};
"""


VALIDATION_PROMOTION_SCHEMA = """
DEFINE FIELD IF NOT EXISTS validation_binding_json ON memory_derivations TYPE option<string>;
DEFINE FIELD IF NOT EXISTS validation_entity_id ON memory_derivations TYPE option<string>;
DEFINE INDEX IF NOT EXISTS memory_derivation_validation_entity ON memory_derivations FIELDS organization_id, validation_entity_id;
DEFINE FIELD IF NOT EXISTS promotion_write_witness ON memory_validation_executions TYPE option<int> DEFAULT 0;
DEFINE EVENT IF NOT EXISTS retain_validation_binding ON memory_derivations WHEN $event='UPDATE'
    AND $before.validation_binding_json!=NONE
    AND ($after.validation_binding_json!=$before.validation_binding_json OR $after.validation_entity_id!=$before.validation_entity_id)
    THEN { THROW 'validation binding is immutable'; };
"""


VALIDATION_RECEIPT_RECOVERY_SCHEMA = """
DEFINE FIELD IF NOT EXISTS recovery_key ON memory_validation_executions TYPE option<string>;
"""

VALIDATION_RECEIPT_PURGE_EVENT = """
DEFINE EVENT OVERWRITE memory_validation_purge ON raw_captures WHEN $event = 'DELETE'
THEN {
    UPDATE memory_validation_executions SET result_json = NONE, recovery_key = NONE, purged = true
        WHERE organization_id = $before.organization_id
            AND (parent_id = $before.uuid OR $before.uuid IN source_ids);
};
"""
