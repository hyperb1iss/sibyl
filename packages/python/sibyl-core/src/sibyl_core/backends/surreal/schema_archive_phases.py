"""Server-only store-local phase evidence and irreversible rollback fences."""

from sibyl_core.backends.surreal.schema_helpers import split_statements
from sibyl_core.migrate.personal_archive_plan import ArchiveKind

ARCHIVE_PHASE_TABLES = ("archive_phase_controls", "archive_phase_receipts")
_KIND_VALUES = "[" + ",".join(repr(kind.value) for kind in ArchiveKind) + "]"

_COMMON_FIELDS = {
    "organization_id": "string",
    "actor_id": "string",
    "run_id": "string",
    "store": "string ASSERT $value IN ['content', 'graph']",
    "binding_json": "string",
    "binding_sha256": "string",
    "checked_plan_sha256": "string",
}
_CONTROL_FIELDS = {
    **_COMMON_FIELDS,
    "revision": "int ASSERT $value >= 0",
    "token": "string",
    "state": "string ASSERT $value IN ['open', 'rolling_back', 'rolled_back']",
}
_RECEIPT_FIELDS = {
    **_COMMON_FIELDS,
    "action": "string ASSERT $value IN ['apply', 'rollback']",
    "phase": "string ASSERT $value IN ['content_apply', 'content_rollback', 'graph_apply', 'graph_rollback']",
    "batch_sequence": "int ASSERT $value >= 0",
    "previous_revision": "int ASSERT $value >= 0",
    "committed_revision": "int ASSERT $value > 0",
    "previous_token": "string",
    "token": "string",
    "terminal": "bool",
    "counts": "array<object> FLEXIBLE",
    "introduced": "array<object> FLEXIBLE",
    "retired": "array<object> FLEXIBLE",
}


def _definitions(table: str, fields: dict[str, str]) -> str:
    return "\n".join(
        (
            f"DEFINE TABLE IF NOT EXISTS {table} SCHEMAFULL;",
            f"ALTER TABLE IF EXISTS {table} SCHEMAFULL;",
            f"ALTER TABLE IF EXISTS {table} PERMISSIONS NONE;",
            *(
                f"DEFINE FIELD IF NOT EXISTS {name} ON {table} TYPE {value};"
                for name, value in fields.items()
            ),
        )
    )


_BINDING_CHANGED = " OR ".join(f"$before.{name} != $after.{name}" for name in _COMMON_FIELDS)

ARCHIVE_PHASE_DEFINITIONS = (
    _definitions("archive_phase_controls", _CONTROL_FIELDS)
    + "\n"
    + _definitions("archive_phase_receipts", _RECEIPT_FIELDS)
    + f"""
DEFINE INDEX IF NOT EXISTS archive_phase_control_run ON archive_phase_controls
    FIELDS organization_id, run_id UNIQUE;
DEFINE INDEX IF NOT EXISTS archive_phase_receipt_batch ON archive_phase_receipts
    FIELDS organization_id, run_id, checked_plan_sha256, phase, batch_sequence UNIQUE;
DEFINE INDEX IF NOT EXISTS archive_phase_receipt_revision ON archive_phase_receipts
    FIELDS organization_id, run_id, checked_plan_sha256, store, committed_revision UNIQUE;
DEFINE INDEX IF NOT EXISTS archive_phase_receipt_owner ON archive_phase_receipts
    FIELDS organization_id, actor_id, run_id, store;
DEFINE EVENT IF NOT EXISTS archive_phase_control_fence ON archive_phase_controls
WHEN true THEN {{
    IF $event = 'DELETE' {{ THROW 'Archive phase history cannot be deleted'; }};
    IF $event = 'CREATE' AND ($after.revision != 0 OR $after.state != 'open') {{
        THROW 'Archive phase control must start open at revision zero';
    }};
    IF $event = 'UPDATE' {{
        IF ({_BINDING_CHANGED}) OR $before.state = 'rolled_back'
            OR $after.revision != $before.revision + 1 {{
            THROW 'Archive phase control binding or revision changed';
        }};
        LET $receipt = SELECT * FROM archive_phase_receipts
            WHERE organization_id = $after.organization_id AND actor_id = $after.actor_id
                AND run_id = $after.run_id AND store = $after.store
                AND binding_sha256 = $after.binding_sha256
                AND committed_revision = $after.revision AND previous_revision = $before.revision
                AND previous_token = $before.token AND token = $after.token;
        IF array::len($receipt) != 1 {{ THROW 'Archive phase revision requires its committed receipt'; }};
        LET $proof = $receipt[0];
        LET $state = IF $proof.action = 'apply' THEN 'open'
            ELSE IF $proof.terminal THEN 'rolled_back' ELSE 'rolling_back' END;
        IF $after.state != $state {{ THROW 'Archive phase state does not match its receipt'; }};
    }};
}};
DEFINE EVENT IF NOT EXISTS archive_phase_receipt_immutable ON archive_phase_receipts
    WHEN $event IN ['UPDATE', 'DELETE'] THEN {{ THROW 'Archive phase receipt is immutable'; }};
DEFINE EVENT IF NOT EXISTS archive_phase_receipt_commit ON archive_phase_receipts
WHEN $event = 'CREATE' THEN {{
    LET $receipt = $after;
    IF !type::is::array($receipt.counts) OR !type::is::array($receipt.introduced)
        OR !type::is::array($receipt.retired) {{ THROW 'Archive phase evidence arrays are missing'; }};
    IF $receipt.phase != string::concat($receipt.store, '_', $receipt.action)
        OR $receipt.committed_revision != $receipt.previous_revision + 1 {{
        THROW 'Archive phase receipt identity or revision is invalid';
    }};
    IF $receipt.action = 'apply' AND ($receipt.terminal OR array::len($receipt.retired) != 0
        OR $receipt.previous_token != $receipt.token) {{
        THROW 'Archive apply cannot claim rollback';
    }};
    IF $receipt.action = 'rollback' AND array::len($receipt.introduced) != 0 {{
        THROW 'Archive rollback cannot introduce rows';
    }};
    LET $count_kinds = $receipt.counts.kind ?? [];
    IF array::len(array::distinct($count_kinds)) != array::len($count_kinds) {{
        THROW 'Archive phase counts repeat a kind';
    }};
    FOR $count IN $receipt.counts {{
        IF $count.kind NOT IN {_KIND_VALUES} {{ THROW 'Archive phase kind is unsupported'; }};
        FOR $number IN [$count.created, $count.skipped, $count.conflicted, $count.quarantined,
            $count.retired, $count.preserved] {{
            IF !type::is::int($number) OR $number < 0 {{ THROW 'Archive phase counts must be integers'; }};
        }};
        IF $count.created != array::len($receipt.introduced[WHERE kind = $count.kind] ?? [])
            OR $count.retired != array::len($receipt.retired[WHERE introduced.kind = $count.kind] ?? []) {{
            THROW 'Archive phase counts do not reconcile';
        }};
        IF ($receipt.action = 'apply' AND ($count.retired != 0 OR $count.preserved != 0))
            OR ($receipt.action = 'rollback' AND ($count.created != 0 OR $count.skipped != 0 OR $count.quarantined != 0)) {{
            THROW 'Archive phase counts claim the wrong action';
        }};
    }};
    IF array::len(array::distinct($receipt.introduced.map(|$row| [$row.kind, $row.destination_id])))
        != array::len($receipt.introduced)
        OR array::len(array::distinct($receipt.retired.map(|$row| [$row.introduced.kind, $row.introduced.destination_id])))
        != array::len($receipt.retired) {{ THROW 'Archive phase repeats an actual identity'; }};
    FOR $row IN $receipt.introduced {{
        LET $actual = IF $row.kind = 'raw_capture' THEN
            (SELECT * FROM raw_captures WHERE organization_id = $receipt.organization_id AND uuid = $row.destination_id)[0]
            ELSE IF $row.kind = 'graph_entity' THEN
            (SELECT * FROM entity WHERE group_id = $receipt.organization_id AND uuid = $row.destination_id)[0]
            ELSE (SELECT * FROM relates_to WHERE group_id = $receipt.organization_id AND uuid = $row.destination_id)[0] END;
        IF $actual = NONE OR type::string($actual.id) != $row.physical_id
            OR crypto::sha256(type::string($actual)) != $row.row_sha256 {{
            THROW 'Archive introduced evidence does not match its actual native row';
        }};
        LET $body = IF $row.kind = 'raw_capture' THEN
            [$actual.title, $actual.raw_content, $actual.metadata, $actual.derivation_required]
            ELSE IF $row.kind = 'graph_entity' THEN
            [$actual.entity_type, $actual.name, $actual.summary, $actual.description, $actual.content,
                $actual.attributes, $actual.derivation_required]
            ELSE [$actual.name, $actual.fact, $actual.attributes, $actual.operational_derivation_required] END;
        LET $audience = IF $row.kind = 'raw_capture' THEN
            [$actual.memory_scope, $actual.scope_key, $actual.principal_id, $actual.project_id]
            ELSE [$actual.memory_scope ?? $actual.attributes.memory_scope, $actual.attributes.scope_key,
                $actual.attributes.principal_id, $actual.project_id] END;
        IF crypto::sha256(type::string($body)) != $row.body_sha256
            OR crypto::sha256(type::string($audience)) != $row.audience_sha256 {{
            THROW 'Archive introduced body or audience evidence changed';
        }};
        IF $row.kind NOT IN $count_kinds OR $row.kind NOT IN ['raw_capture', 'graph_entity', 'graph_relationship']
            OR !type::is::string($row.destination_id) OR string::len($row.destination_id) = 0
            OR !type::is::string($row.physical_id)
            OR !type::is::string($row.row_sha256) OR string::len($row.row_sha256) != 64
            OR !type::is::string($row.body_sha256) OR string::len($row.body_sha256) != 64
            OR !type::is::string($row.audience_sha256) OR string::len($row.audience_sha256) != 64 {{
            THROW 'Archive introduced row evidence is missing';
        }};
        IF $row.kind = 'graph_relationship' {{
            LET $endpoints = [
                (SELECT * FROM entity WHERE id = $actual.in AND group_id = $receipt.organization_id)[0],
                (SELECT * FROM entity WHERE id = $actual.out AND group_id = $receipt.organization_id)[0]];
            LET $states = [
                (SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id = $receipt.organization_id AND source_kind = 'graph_entity' AND source_id = $endpoints[0].uuid)[0],
                (SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id = $receipt.organization_id AND source_kind = 'graph_entity' AND source_id = $endpoints[1].uuid)[0]];
            IF $endpoints[0] = NONE OR $endpoints[1] = NONE OR $states[0] = NONE OR $states[1] = NONE
                OR $states[0].deleted OR $states[1].deleted
                OR !type::is::string($states[0].incarnation) OR string::len($states[0].incarnation) = 0
        OR !type::is::int($states[0].generation) OR $states[0].generation < 1
        OR $states[0].revision != $endpoints[0].revision
                OR !type::is::string($states[1].incarnation) OR string::len($states[1].incarnation) = 0
        OR !type::is::int($states[1].generation) OR $states[1].generation < 1
        OR $states[1].revision != $endpoints[1].revision
                OR $endpoints.uuid != $row.endpoint_ids
                OR [crypto::sha256(type::string($states[0])), crypto::sha256(type::string($states[1]))] != $row.endpoint_state_sha256
                OR crypto::sha256(type::string([$actual.in, $actual.out, $actual.source_id, $actual.target_id, $actual.operational_source_binding])) != $row.binding_sha256 {{
                THROW 'Archive edge endpoint or binding evidence changed';
            }};
            IF $receipt.store != 'graph' OR $row.revision != NULL OR $row.source_incarnation != NULL
                OR $row.source_generation != NULL OR $row.source_state_revision != NULL
                OR array::len($row.endpoint_ids) != 2 OR array::len($row.endpoint_state_sha256) != 2
                OR !type::is::string($row.binding_sha256) OR string::len($row.binding_sha256) != 64 {{
                THROW 'Archive relationship evidence cannot invent a source state';
            }};
        }} ELSE {{
            LET $source = (SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id = $receipt.organization_id
                AND source_kind = $row.kind AND source_id = $row.destination_id)[0];
            IF $source = NONE OR $source.deleted OR $source.incarnation != $row.source_incarnation
                OR $source.generation != $row.source_generation OR $source.revision != $row.source_state_revision
                OR $actual.revision != $row.revision {{ THROW 'Archive introduced source evidence changed'; }};
            IF ($row.kind = 'raw_capture' AND $receipt.store != 'content')
                OR ($row.kind = 'graph_entity' AND $receipt.store != 'graph')
                OR !type::is::int($row.revision) OR $row.revision < 1
                OR !type::is::string($row.source_incarnation) OR string::len($row.source_incarnation) = 0
                OR !type::is::int($row.source_generation) OR $row.source_generation < 1
                OR $row.source_state_revision != $row.revision {{
                THROW 'Archive canonical source evidence is missing';
            }};
        }};
    }};
    FOR $retirement IN $receipt.retired {{
        LET $introduced = $retirement.introduced;
        LET $proof = SELECT * FROM archive_phase_receipts WHERE organization_id = $receipt.organization_id
            AND actor_id = $receipt.actor_id AND run_id = $receipt.run_id AND store = $receipt.store
            AND binding_sha256 = $receipt.binding_sha256 AND action = 'apply' AND array::len(introduced[WHERE (kind ?? NULL) = ($introduced.kind ?? NULL) AND (destination_id ?? NULL) = ($introduced.destination_id ?? NULL) AND (physical_id ?? NULL) = ($introduced.physical_id ?? NULL) AND (row_sha256 ?? NULL) = ($introduced.row_sha256 ?? NULL) AND (body_sha256 ?? NULL) = ($introduced.body_sha256 ?? NULL) AND (audience_sha256 ?? NULL) = ($introduced.audience_sha256 ?? NULL) AND (revision ?? NULL) = ($introduced.revision ?? NULL) AND (source_incarnation ?? NULL) = ($introduced.source_incarnation ?? NULL) AND (source_generation ?? NULL) = ($introduced.source_generation ?? NULL) AND (source_state_revision ?? NULL) = ($introduced.source_state_revision ?? NULL) AND (endpoint_ids ?? NULL) = ($introduced.endpoint_ids ?? NULL) AND (endpoint_state_sha256 ?? NULL) = ($introduced.endpoint_state_sha256 ?? NULL) AND (binding_sha256 ?? NULL) = ($introduced.binding_sha256 ?? NULL)] ?? []) = 1;
        IF array::len($proof) != 1 {{ THROW 'Archive retirement has no retained introduced receipt'; }};
        LET $row = (SELECT * FROM type::record($introduced.physical_id))[0];
        IF ($row = NONE) != $retirement.absent
            OR ($row != NONE AND crypto::sha256(type::string($row)) != $retirement.row_sha256) {{
            THROW 'Archive retirement row evidence changed';
        }};
        IF $retirement.introduced.kind NOT IN $count_kinds {{ THROW 'Archive retirement counts are missing'; }};
        IF $retirement.introduced.kind = 'graph_relationship' {{
            IF !$retirement.absent OR $retirement.row_sha256 != NULL
                OR $retirement.source_incarnation != NULL OR $retirement.source_generation != NULL
                OR $retirement.source_state_sha256 != NULL {{
                THROW 'Archive edge retirement must retain absence evidence';
            }};
        }} ELSE {{
            LET $state = (SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id = $receipt.organization_id
                AND source_kind = $introduced.kind AND source_id = $introduced.destination_id)[0];
            IF $state = NONE OR !$state.deleted OR $state.incarnation != $retirement.source_incarnation
                OR $state.generation != $retirement.source_generation
                OR crypto::sha256(type::string($state)) != $retirement.source_state_sha256 {{
                THROW 'Archive retirement source evidence changed';
            }};
            IF $retirement.source_incarnation != $retirement.introduced.source_incarnation
                OR !type::is::int($retirement.source_generation)
                OR $retirement.source_generation <= $retirement.introduced.source_generation
                OR !type::is::string($retirement.source_state_sha256) {{
                THROW 'Archive retirement must preserve its retained high-water';
            }};
        }};
    }};
    LET $control = (SELECT * FROM archive_phase_controls
        WHERE organization_id = $receipt.organization_id AND actor_id = $receipt.actor_id
            AND run_id = $receipt.run_id AND store = $receipt.store)[0];
    IF $control = NONE OR $control.binding_json != $receipt.binding_json
        OR $control.binding_sha256 != $receipt.binding_sha256
        OR $control.checked_plan_sha256 != $receipt.checked_plan_sha256
        OR $control.revision != $receipt.previous_revision OR $control.token != $receipt.previous_token
        OR $control.state = 'rolled_back' {{ THROW 'Archive phase control changed'; }};
    IF ($receipt.action = 'apply' AND $control.state != 'open')
        OR ($receipt.action = 'rollback' AND $control.state = 'open' AND $receipt.token = $control.token)
        OR ($receipt.action = 'rollback' AND $control.state = 'rolling_back' AND $receipt.token != $control.token) {{
        THROW 'Archive phase token is closed';
    }};
    LET $advanced = UPDATE $control.id SET revision = $receipt.committed_revision,
        token = $receipt.token,
        state = IF $receipt.action = 'apply' THEN 'open'
            ELSE IF $receipt.terminal THEN 'rolled_back' ELSE 'rolling_back' END
        WHERE revision = $receipt.previous_revision AND token = $receipt.previous_token RETURN AFTER;
    IF array::len($advanced) != 1 {{ THROW 'Archive phase control changed'; }};
}};
"""
)


# Keep compound event bodies intact while exposing each unique index to the
# schema invariant checker. All delimiters belong to these fixed definitions.
_header, *_events = ARCHIVE_PHASE_DEFINITIONS.split("\nDEFINE EVENT ")
ARCHIVE_PHASE_STATEMENTS = (
    *split_statements(_header),
    *("DEFINE EVENT " + event.strip() for event in _events),
)
