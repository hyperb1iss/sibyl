"""Server-private shadow decisions, independent of memory publication."""

DECISION_SCHEMA = """
DEFINE TABLE IF NOT EXISTS semantic_decision_policies SCHEMAFULL;
ALTER TABLE IF EXISTS semantic_decision_policies SCHEMAFULL;
ALTER TABLE IF EXISTS semantic_decision_policies PERMISSIONS NONE;
DEFINE FIELD IF NOT EXISTS uuid ON semantic_decision_policies TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON semantic_decision_policies TYPE string;
DEFINE FIELD IF NOT EXISTS principal_id ON semantic_decision_policies TYPE string;
DEFINE FIELD IF NOT EXISTS project_id ON semantic_decision_policies TYPE option<string>;
DEFINE FIELD IF NOT EXISTS epoch ON semantic_decision_policies TYPE int ASSERT $value > 0;
DEFINE FIELD IF NOT EXISTS enabled ON semantic_decision_policies TYPE bool;
DEFINE FIELD IF NOT EXISTS policy_version ON semantic_decision_policies TYPE string;
DEFINE FIELD IF NOT EXISTS route_policy_sha256 ON semantic_decision_policies TYPE string;
DEFINE INDEX IF NOT EXISTS semantic_decision_policy_uuid ON semantic_decision_policies FIELDS uuid UNIQUE;
DEFINE TABLE IF NOT EXISTS semantic_decision_source_fences SCHEMAFULL;
ALTER TABLE IF EXISTS semantic_decision_source_fences SCHEMAFULL;
ALTER TABLE IF EXISTS semantic_decision_source_fences PERMISSIONS NONE;
DEFINE FIELD IF NOT EXISTS organization_id ON semantic_decision_source_fences TYPE string;
DEFINE FIELD IF NOT EXISTS source_id ON semantic_decision_source_fences TYPE string;
DEFINE FIELD IF NOT EXISTS witness ON semantic_decision_source_fences TYPE string;
DEFINE INDEX IF NOT EXISTS semantic_decision_source_fence_identity ON semantic_decision_source_fences FIELDS organization_id, source_id UNIQUE;
DEFINE TABLE IF NOT EXISTS semantic_decision_receipts SCHEMAFULL;
ALTER TABLE IF EXISTS semantic_decision_receipts SCHEMAFULL;
ALTER TABLE IF EXISTS semantic_decision_receipts PERMISSIONS NONE;
DEFINE FIELD IF NOT EXISTS uuid ON semantic_decision_receipts TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON semantic_decision_receipts TYPE string;
DEFINE FIELD IF NOT EXISTS principal_id ON semantic_decision_receipts TYPE string;
DEFINE FIELD IF NOT EXISTS parent_id ON semantic_decision_receipts TYPE string;
DEFINE FIELD IF NOT EXISTS source_ids ON semantic_decision_receipts TYPE array<string>;
DEFINE FIELD IF NOT EXISTS policy_key ON semantic_decision_receipts TYPE string;
DEFINE FIELD IF NOT EXISTS policy_epoch ON semantic_decision_receipts TYPE int;
DEFINE FIELD IF NOT EXISTS request_digest ON semantic_decision_receipts TYPE string;
DEFINE FIELD IF NOT EXISTS semantic_digest ON semantic_decision_receipts TYPE string;
DEFINE FIELD IF NOT EXISTS state ON semantic_decision_receipts TYPE string
    ASSERT $value IN ['pending', 'completed', 'unavailable', 'stale', 'cancelled', 'failed'];
DEFINE FIELD IF NOT EXISTS attempt_count ON semantic_decision_receipts TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS observation_json ON semantic_decision_receipts TYPE option<string>;
DEFINE FIELD IF NOT EXISTS usage_json ON semantic_decision_receipts TYPE option<string>;
DEFINE FIELD IF NOT EXISTS purged ON semantic_decision_receipts TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS created_at ON semantic_decision_receipts TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS semantic_decision_receipt_uuid ON semantic_decision_receipts FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS semantic_decision_receipt_owner ON semantic_decision_receipts FIELDS organization_id, principal_id, parent_id;
"""

DECISION_PURGE_EVENT = """
DEFINE EVENT IF NOT EXISTS semantic_decision_purge ON raw_captures
    WHEN $event = 'DELETE' OR ($event = 'UPDATE' AND $after.deleted_at != NONE)
    THEN {
        LET $fence_id=type::record(string::concat('semantic_decision_source_fences:',
            crypto::sha256(type::string([$before.organization_id, $before.uuid]))));
        UPSERT $fence_id SET organization_id=$before.organization_id, source_id=$before.uuid,
            witness=type::string(rand::uuid());
        UPDATE semantic_decision_receipts SET observation_json = NONE, purged = true, state = 'stale'
            WHERE organization_id = $before.organization_id
                AND (parent_id = $before.uuid OR $before.uuid IN source_ids);
    };
"""

# Fences exist before any decision invocation and outlive source deletion. Their
# only purpose is to order receipt insertion against a concurrent purge scan.
DECISION_SOURCE_FENCE_CREATE_EVENT = """
DEFINE EVENT IF NOT EXISTS semantic_decision_source_fence ON raw_captures
    WHEN $event = 'CREATE' THEN {
        LET $fence_id=type::record(string::concat('semantic_decision_source_fences:',
            crypto::sha256(type::string([$after.organization_id, $after.uuid]))));
        UPSERT $fence_id SET organization_id=$after.organization_id, source_id=$after.uuid,
            witness=type::string(rand::uuid());
    };
"""

DECISION_SOURCE_FENCE_BACKFILL = """
LET $decision_backfill_sources=(SELECT organization_id, uuid FROM raw_captures);
FOR $source IN $decision_backfill_sources {
    LET $fence_id=type::record(string::concat('semantic_decision_source_fences:',
        crypto::sha256(type::string([$source.organization_id, $source.uuid]))));
    UPSERT $fence_id SET organization_id=$source.organization_id, source_id=$source.uuid,
        witness=type::string(rand::uuid());
};
"""
