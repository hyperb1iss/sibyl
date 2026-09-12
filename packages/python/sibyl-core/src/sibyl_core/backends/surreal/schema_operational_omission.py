"""A retained omitted projection loses regeneration permission on deletion."""

OPERATIONAL_OMISSION_STATEMENTS = (
    "DEFINE FIELD IF NOT EXISTS operational_omission ON memory_derivations "
    "TYPE option<object> FLEXIBLE;",
    """DEFINE EVENT IF NOT EXISTS clear_operational_omission ON entity
    WHEN $event = 'DELETE' THEN {
        UPDATE memory_derivations SET operational_omission = NONE
            WHERE organization_id = $before.group_id AND target_kind = 'graph_entity'
                AND target_id = $before.uuid;
    };""",
)
