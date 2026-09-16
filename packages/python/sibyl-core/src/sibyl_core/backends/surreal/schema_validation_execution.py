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
DEFINE INDEX IF NOT EXISTS memory_validation_execution_owner ON memory_validation_executions FIELDS parent_id;
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

VALIDATION_PROVIDER_ERROR_SCHEMA = """
DEFINE FIELD IF NOT EXISTS error_detail ON memory_validation_executions TYPE option<string>;
"""

VALIDATION_ORIGIN_SCHEMA = """
DEFINE FIELD IF NOT EXISTS origin_execution_id ON memory_derivations TYPE option<string>;
DEFINE EVENT IF NOT EXISTS retain_derivation_origin ON memory_derivations WHEN $event='UPDATE'
    AND $before.origin_execution_id!=$after.origin_execution_id
    THEN { THROW 'Derivation origin is immutable'; };
"""

VALIDATION_RECEIPT_PURGE_EVENT = """
DEFINE EVENT OVERWRITE memory_validation_purge ON raw_captures WHEN $event = 'DELETE'
THEN {
    UPDATE memory_validation_executions SET result_json = NONE, recovery_key = NONE, purged = true
        WHERE organization_id = $before.organization_id
            AND (parent_id = $before.uuid OR $before.uuid IN source_ids);
};
"""


VALIDATION_DEPENDENCY_SCHEMA = """
DEFINE FIELD IF NOT EXISTS dependency_ids ON memory_validation_executions TYPE array<string> DEFAULT [];
DEFINE INDEX IF NOT EXISTS memory_validation_dependencies ON memory_validation_executions FIELDS dependency_ids;
"""

VALIDATION_DEPENDENCY_RETAIN_EVENT = """
DEFINE EVENT IF NOT EXISTS retain_validation_dependencies ON memory_validation_executions
    WHEN $event='UPDATE' AND ($before.dependency_ids ?? [])!=($after.dependency_ids ?? [])
    THEN { THROW 'Validation dependencies are immutable'; };
"""

VALIDATION_DEPENDENCY_PURGE_EVENT = """
DEFINE EVENT IF NOT EXISTS purge_validation_dependents ON memory_validation_executions
    WHEN $event='DELETE' OR ($event='UPDATE' AND $before.purged=false AND $after.purged=true)
    THEN {
        LET $dependent_records=(SELECT id, uuid, array::len(dependency_ids) AS depth FROM memory_validation_executions
            WHERE organization_id=$before.organization_id AND principal_id=$before.principal_id
                AND $before.uuid IN dependency_ids AND purged=false ORDER BY depth DESC, uuid);
        FOR $dependent IN $dependent_records {
            UPDATE $dependent.id SET result_json=NONE, recovery_key=NONE, purged=true;
        };
    };
"""


VALIDATION_OWNER_INDEX_REPAIR = """
DEFINE INDEX OVERWRITE memory_validation_execution_owner ON memory_validation_executions FIELDS parent_id;
"""


VALIDATION_DEPENDENCY_SOURCE_PURGE_EVENT = """
DEFINE EVENT OVERWRITE memory_validation_purge ON raw_captures WHEN $event='DELETE'
THEN {
    LET $source_records=(SELECT id, uuid, array::len(dependency_ids) AS depth
        FROM memory_validation_executions WHERE organization_id=$before.organization_id
            AND (parent_id=$before.uuid OR $before.uuid IN source_ids)
        ORDER BY depth DESC, uuid);
    FOR $source_record IN $source_records {
        UPDATE $source_record.id SET result_json=NONE, recovery_key=NONE, purged=true;
    };
};
"""


VALIDATION_DEPENDENCY_UPGRADE = """
DEFINE FIELD IF NOT EXISTS dependency_ids ON memory_validation_executions TYPE option<array<string>> DEFAULT [];
UPDATE memory_validation_executions SET dependency_ids=[] WHERE dependency_ids=NONE;
DEFINE FIELD OVERWRITE dependency_ids ON memory_validation_executions TYPE array<string> DEFAULT [];
DEFINE INDEX IF NOT EXISTS memory_validation_dependencies ON memory_validation_executions FIELDS dependency_ids;
"""
