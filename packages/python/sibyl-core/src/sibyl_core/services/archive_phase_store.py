"""Internal transaction composition for store-local archive phase evidence.

Writers supply trusted application SQL and actual outcome branches. This module
proves declared identities absent before those writes, reads actual rows after
them using fixed canonical tables, and
creates the receipt in that transaction. It deliberately exposes no executor,
standalone completion method, active import writer, or authorization shortcut.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from sibyl_core.backends.surreal.connection import _query_tokens
from sibyl_core.backends.surreal.schema import render_surreal_compatible_sql
from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.backends.surreal.schema_version import SurrealExecute
from sibyl_core.migrate.archive_phase_receipts import (
    ArchiveCreatedIdentity,
    ArchivePhaseControl,
    ArchivePhaseKey,
    ArchivePhaseReceipt,
    IntroducedArchiveRow,
    phase_binding_json,
    strict_phase_json,
)

_PREFIX = "sibyl_archive_phase_"
_UNUSED_STORE_CLOSE_WRITER = "LET $sibyl_archive_phase_outcomes = [];"

# The native encoding retains datetime precision and physical record identity.
# These fingerprints are deliberately distinct from checked semantic digests.
# Map contexts are explicit because embedded closures do not capture outer LETs.
_POST_READS = """
LET $sibyl_archive_phase_post_raw = SELECT * FROM raw_captures WHERE organization_id = $sibyl_archive_phase_org
    AND uuid IN ($sibyl_archive_phase_outcomes.destination_id ?? []);
LET $sibyl_archive_phase_post_entities = SELECT * FROM entity WHERE group_id = $sibyl_archive_phase_org
    AND uuid IN ($sibyl_archive_phase_outcomes.destination_id ?? []);
LET $sibyl_archive_phase_post_edges = SELECT * FROM relates_to WHERE group_id = $sibyl_archive_phase_org
    AND uuid IN ($sibyl_archive_phase_outcomes.destination_id ?? []);
LET $sibyl_archive_phase_post_endpoints = SELECT * FROM entity WHERE group_id = $sibyl_archive_phase_org
    AND id IN array::concat($sibyl_archive_phase_post_edges.in ?? [], $sibyl_archive_phase_post_edges.out ?? []);
LET $sibyl_archive_phase_post_states = SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id = $sibyl_archive_phase_org
    AND source_id IN array::concat($sibyl_archive_phase_outcomes.destination_id ?? [], $sibyl_archive_phase_post_endpoints.uuid ?? []);

"""

_INTRODUCED_READ = """
LET $sibyl_archive_phase_introduced = array::combine($sibyl_archive_phase_outcomes[WHERE disposition = 'created'] ?? [], [{
    cut: $sibyl_archive_phase_presence_cut, raw: $sibyl_archive_phase_post_raw,
    entities: $sibyl_archive_phase_post_entities, edges: $sibyl_archive_phase_post_edges,
    endpoints: $sibyl_archive_phase_post_endpoints, states: $sibyl_archive_phase_post_states
}]).map(|$pair| {
    LET $outcome = $pair[0];
    LET $context = $pair[1];
    LET $cut = $context.cut[WHERE kind = $outcome.kind AND destination_id = $outcome.destination_id][0];
    IF $cut = NONE { THROW 'Archive created outcome was not absent in its declared cut'; };
    IF $outcome.kind NOT IN ['raw_capture', 'graph_entity', 'graph_relationship'] {
        THROW 'Archive quarantine cannot claim an active identity';
    };
    LET $row = IF $outcome.kind = 'raw_capture' THEN
        $context.raw[WHERE uuid = $outcome.destination_id][0]
        ELSE IF $outcome.kind = 'graph_entity' THEN
        $context.entities[WHERE uuid = $outcome.destination_id][0]
        ELSE $context.edges[WHERE uuid = $outcome.destination_id][0] END;
    IF $row = NONE { THROW 'Archive introduced native row is missing'; };
    LET $source = IF $outcome.kind = 'graph_relationship' THEN NULL ELSE
        $context.states[WHERE source_kind = $outcome.kind AND source_id = $outcome.destination_id][0] END;
    IF $outcome.kind != 'graph_relationship' AND ($source = NONE OR $source.deleted
        OR $source.revision != $row.revision OR !type::is::int($source.generation)
        OR $source.generation < 1 OR !type::is::string($source.incarnation)
        OR string::len($source.incarnation) = 0) {
        THROW 'Archive introduced native source state is missing';
    };
    LET $endpoints = IF $outcome.kind = 'graph_relationship' THEN [
        $context.endpoints[WHERE id = $row.in][0],
        $context.endpoints[WHERE id = $row.out][0]
    ] ELSE [] END;
    LET $endpoint_states = IF $outcome.kind = 'graph_relationship' THEN [
        $context.states[WHERE source_kind = 'graph_entity' AND source_id = $endpoints[0].uuid][0],
        $context.states[WHERE source_kind = 'graph_entity' AND source_id = $endpoints[1].uuid][0]
    ] ELSE [] END;
    IF $outcome.kind = 'graph_relationship' AND ($endpoints[0] = NONE OR $endpoints[1] = NONE
        OR $endpoint_states[0] = NONE OR $endpoint_states[1] = NONE
        OR $endpoint_states[0].deleted OR $endpoint_states[1].deleted
        OR !type::is::string($endpoint_states[0].incarnation) OR string::len($endpoint_states[0].incarnation) = 0
        OR !type::is::int($endpoint_states[0].generation) OR $endpoint_states[0].generation < 1
        OR $endpoint_states[0].revision != $endpoints[0].revision
        OR !type::is::string($endpoint_states[1].incarnation) OR string::len($endpoint_states[1].incarnation) = 0
        OR !type::is::int($endpoint_states[1].generation) OR $endpoint_states[1].generation < 1
        OR $endpoint_states[1].revision != $endpoints[1].revision) {
        THROW 'Archive introduced edge endpoint is unavailable';
    };
    LET $body = IF $outcome.kind = 'raw_capture' THEN
        [$row.title, $row.raw_content, $row.metadata, $row.derivation_required]
        ELSE IF $outcome.kind = 'graph_entity' THEN
        [$row.entity_type, $row.name, $row.summary, $row.description, $row.content,
            $row.attributes, $row.derivation_required]
        ELSE [$row.name, $row.fact, $row.attributes, $row.operational_derivation_required] END;
    LET $audience = IF $outcome.kind = 'raw_capture' THEN
        [$row.memory_scope, $row.scope_key, $row.principal_id, $row.project_id]
        ELSE [$row.memory_scope ?? $row.attributes.memory_scope, $row.attributes.scope_key,
            $row.attributes.principal_id, $row.project_id] END;
    LET $evidence = {
        kind: $outcome.kind, destination_id: $row.uuid, physical_id: type::string($row.id),
        row_sha256: crypto::sha256(type::string($row)),
        body_sha256: crypto::sha256(type::string($body)),
        audience_sha256: crypto::sha256(type::string($audience)),
        revision: IF $outcome.kind = 'graph_relationship' THEN NULL ELSE $row.revision END,
        source_incarnation: IF $source = NULL THEN NULL ELSE $source.incarnation END,
        source_generation: IF $source = NULL THEN NULL ELSE $source.generation END,
        source_state_revision: IF $source = NULL THEN NULL ELSE $source.revision END,
        endpoint_ids: $endpoints.uuid ?? [],
        endpoint_state_sha256: IF $outcome.kind = 'graph_relationship' THEN
            [crypto::sha256(type::string($endpoint_states[0])), crypto::sha256(type::string($endpoint_states[1]))]
            ELSE [] END,
        binding_sha256: IF $outcome.kind = 'graph_relationship' THEN
            crypto::sha256(type::string([$row.in, $row.out, $row.source_id, $row.target_id,
                $row.operational_source_binding])) ELSE NULL END
    };
    RETURN $evidence;
});
"""

_RETIREMENT_READ = """
LET $sibyl_archive_phase_retired = array::combine($sibyl_archive_phase_outcomes[WHERE disposition = 'retired'] ?? [], [{
    candidates: $sibyl_archive_phase_retirement_candidates, raw: $sibyl_archive_phase_post_raw,
    entities: $sibyl_archive_phase_post_entities, edges: $sibyl_archive_phase_post_edges,
    states: $sibyl_archive_phase_post_states
}]).map(|$pair| {
    LET $outcome = $pair[0];
    LET $context = $pair[1];
    LET $introduced = $context.candidates[
        WHERE kind = $outcome.kind AND destination_id = $outcome.destination_id][0];
    IF $introduced = NONE { THROW 'Archive retirement has no introduced manifest'; };
    LET $rows = IF $introduced.kind = 'raw_capture' THEN $context.raw
        ELSE IF $introduced.kind = 'graph_entity' THEN $context.entities ELSE $context.edges END;
    LET $row = $rows[WHERE uuid = $introduced.destination_id][0];
    LET $source = IF $introduced.kind = 'graph_relationship' THEN NULL ELSE
        $context.states[WHERE source_kind = $introduced.kind AND source_id = $introduced.destination_id][0] END;
    IF $introduced.kind = 'graph_relationship' AND $row != NONE {
        THROW 'Archive retired relationship still exists';
    };
    IF $introduced.kind != 'graph_relationship' AND ($source = NONE OR !$source.deleted
        OR $source.incarnation != $introduced.source_incarnation
        OR $source.generation <= $introduced.source_generation) {
        THROW 'Archive retirement must retain its native tombstone';
    };
    LET $evidence = {
        introduced: {kind: $introduced.kind, destination_id: $introduced.destination_id,
            physical_id: $introduced.physical_id, row_sha256: $introduced.row_sha256,
            body_sha256: $introduced.body_sha256, audience_sha256: $introduced.audience_sha256,
            revision: $introduced.revision ?? NULL, source_incarnation: $introduced.source_incarnation ?? NULL,
            source_generation: $introduced.source_generation ?? NULL,
            source_state_revision: $introduced.source_state_revision ?? NULL,
            endpoint_ids: $introduced.endpoint_ids, endpoint_state_sha256: $introduced.endpoint_state_sha256,
            binding_sha256: $introduced.binding_sha256 ?? NULL}, absent: $row = NONE,
        row_sha256: IF $row = NONE THEN NULL ELSE crypto::sha256(type::string($row)) END,
        source_incarnation: IF $source = NULL THEN NULL ELSE $source.incarnation END,
        source_generation: IF $source = NULL THEN NULL ELSE $source.generation END,
        source_state_sha256: IF $source = NULL THEN NULL ELSE crypto::sha256(type::string($source)) END
    };
    RETURN $evidence;
});
"""

_OUTCOME_GUARD_AND_COUNTS = """
IF !type::is::array($sibyl_archive_phase_outcomes) { THROW 'Archive writer outcomes are missing'; };
FOR $outcome IN $sibyl_archive_phase_outcomes {
    IF $outcome.disposition NOT IN ['created', 'skipped', 'conflicted', 'quarantined', 'retired', 'preserved']
        OR !type::is::string($outcome.kind) {
        THROW 'Archive writer outcome is invalid';
    };
    IF $outcome.disposition IN ['created', 'retired'] AND
        (!type::is::string($outcome.destination_id) OR string::len($outcome.destination_id) = 0) {
        THROW 'Archive actual mutation identity is missing';
    };
    IF $outcome.disposition = 'quarantined' AND $outcome.destination_id != NONE {
        THROW 'Archive quarantine cannot claim an active identity';
    };
};
LET $sibyl_archive_phase_counts = array::combine(array::distinct($sibyl_archive_phase_outcomes.kind ?? []), [$sibyl_archive_phase_outcomes]).map(|$pair| {
    LET $kind = $pair[0];
    LET $rows = $pair[1][WHERE kind = $kind];
    LET $count = { kind: $kind,
        created: array::len($rows[WHERE disposition = 'created'] ?? []),
        skipped: array::len($rows[WHERE disposition = 'skipped'] ?? []),
        conflicted: array::len($rows[WHERE disposition = 'conflicted'] ?? []),
        quarantined: array::len($rows[WHERE disposition = 'quarantined'] ?? []),
        retired: array::len($rows[WHERE disposition = 'retired'] ?? []),
        preserved: array::len($rows[WHERE disposition = 'preserved'] ?? []) };
    RETURN $count;
});
"""


def _store_local_reads(preamble: str, store: str) -> str:
    """Emit fixed read statements only for this canonical store inventory."""
    statements = []
    for statement in preamble.strip().split(";"):
        if not statement.strip():
            continue
        variable, expression = statement.split(" = ", 1)
        foreign = (
            store == "content"
            and (" FROM entity " in expression or " FROM relates_to " in expression)
        ) or (store == "graph" and " FROM raw_captures " in expression)
        statements.append(variable + " = " + ("[]" if foreign else expression) + ";")
    return "\n".join(statements) + "\n"


@dataclass(frozen=True, slots=True)
class PreparedArchivePhaseTransaction:
    """Exact query and immutable JSON parameters for the native capacity guard."""

    query: str
    parameters_json: str

    @property
    def parameters(self) -> dict[str, object]:
        # Each access returns a fresh snapshot, including nested writer values.
        return cast(dict[str, object], json.loads(self.parameters_json))


def prepare_archive_phase_transaction(
    *,
    key: ArchivePhaseKey,
    url: str,
    expected_revision: int,
    expected_token: str,
    writer_statements: str,
    writer_parameters: dict[str, object],
    rollback_token: str | None = None,
    terminal: bool = False,
    retirement_candidates: tuple[IntroducedArchiveRow, ...] = (),
    planned_creates: tuple[ArchiveCreatedIdentity, ...] = (),
    _unused_store_close: bool = False,
) -> PreparedArchivePhaseTransaction:
    """Compose only trusted, server-owned writer SQL with its native receipt.

    The writer declares its actual branches in $sibyl_archive_phase_outcomes.
    This is an internal composition contract, never a client SQL interface.
    Canonical eligibility/current authorization remain the writer's responsibility.
    """
    key = ArchivePhaseKey.model_validate(key.model_dump(mode="python"))
    binding = key.binding
    control = ArchivePhaseControl(
        binding=binding,
        store=key.store,
        revision=expected_revision,
        token=expected_token,
        state="open",
    )
    if type(terminal) is not bool or (terminal and key.action != "rollback"):
        raise ValueError("only rollback can finish a terminal phase")
    if key.action == "apply" and (rollback_token is not None or retirement_candidates):
        raise ValueError("apply cannot claim rollback evidence")
    next_token = control.token if rollback_token is None else rollback_token
    ArchivePhaseControl(
        binding=binding, store=key.store, revision=expected_revision, token=next_token, state="open"
    )
    if not writer_statements.strip().strip(";").strip():
        raise ValueError("archive writer statements cannot be empty")
    if {
        "BEGIN",
        "COMMIT",
        "CANCEL",
        "ARCHIVE_PHASE_CONTROLS",
        "ARCHIVE_PHASE_RECEIPTS",
    }.intersection(_query_tokens(writer_statements)):
        raise ValueError("archive writer statements cannot own transaction or receipt boundaries")
    if any(name.startswith(_PREFIX) for name in writer_parameters):
        raise ValueError("archive writer parameters overlap reserved phase parameters")
    candidates = tuple(
        IntroducedArchiveRow.model_validate(row.model_dump(mode="python"))
        for row in retirement_candidates
    )
    planned = tuple(
        ArchiveCreatedIdentity.model_validate(row.model_dump(mode="python"))
        for row in planned_creates
    )
    if len({(row.kind, row.destination_id) for row in planned}) != len(planned):
        raise ValueError("archive planned creates repeat an identity")
    if any(key.store != ("content" if row.kind == "raw_capture" else "graph") for row in planned):
        raise ValueError("archive planned creates belong to another store")
    if key.action != "apply" and planned:
        raise ValueError("archive rollback cannot plan creates")
    if type(_unused_store_close) is not bool or (
        _unused_store_close
        and (
            key.action != "rollback"
            or key.batch_sequence != 0
            or expected_revision != 0
            or not terminal
            or rollback_token is None
            or rollback_token == expected_token
            or candidates
            or planned
            or writer_parameters
            or writer_statements != _UNUSED_STORE_CLOSE_WRITER
        )
    ):
        raise ValueError("unused archive store closure must be a fixed empty terminal phase")
    parameters = {
        **writer_parameters,
        _PREFIX + "org": binding.organization_id,
        _PREFIX + "actor": binding.actor_id,
        _PREFIX + "run": binding.run_id,
        _PREFIX + "store": key.store,
        _PREFIX + "action": key.action,
        _PREFIX + "phase": key.phase,
        _PREFIX + "batch": key.batch_sequence,
        _PREFIX + "binding_json": phase_binding_json(binding),
        _PREFIX + "binding_sha": binding.sha256,
        _PREFIX + "plan_sha": binding.checked_plan_sha256,
        _PREFIX + "revision": control.revision,
        _PREFIX + "token": control.token,
        _PREFIX + "next_token": next_token,
        _PREFIX + "terminal": terminal,
        _PREFIX + "unused_store_close": _unused_store_close,
        _PREFIX + "planned_creates": [row.model_dump(mode="json") for row in planned],
        _PREFIX + "retirement_candidates": [row.model_dump(mode="json") for row in candidates],
    }
    parameters_json = strict_phase_json(parameters)
    query = """BEGIN TRANSACTION;
IF $sibyl_archive_phase_unused_store_close {
    LET $sibyl_archive_phase_existing_receipts = SELECT id FROM archive_phase_receipts
        WHERE organization_id = $sibyl_archive_phase_org AND run_id = $sibyl_archive_phase_run
            AND store = $sibyl_archive_phase_store LIMIT 1;
    IF array::len($sibyl_archive_phase_existing_receipts) != 0 {
        THROW 'Archive store already has committed phase evidence';
    };
};
LET $sibyl_archive_phase_control = (SELECT * FROM archive_phase_controls
    WHERE organization_id = $sibyl_archive_phase_org AND run_id = $sibyl_archive_phase_run)[0];
IF $sibyl_archive_phase_control = NONE {
    IF $sibyl_archive_phase_revision != 0 OR ($sibyl_archive_phase_action != 'apply'
        AND !$sibyl_archive_phase_unused_store_close) {
        THROW 'Archive phase control is missing';
    };
    CREATE archive_phase_controls CONTENT {
        organization_id: $sibyl_archive_phase_org, actor_id: $sibyl_archive_phase_actor,
        run_id: $sibyl_archive_phase_run, store: $sibyl_archive_phase_store,
        binding_json: $sibyl_archive_phase_binding_json, binding_sha256: $sibyl_archive_phase_binding_sha,
        checked_plan_sha256: $sibyl_archive_phase_plan_sha,
        revision: 0, token: $sibyl_archive_phase_token, state: 'open'
    };
};
LET $sibyl_archive_phase_current = (SELECT * FROM archive_phase_controls
    WHERE organization_id = $sibyl_archive_phase_org AND actor_id = $sibyl_archive_phase_actor
        AND run_id = $sibyl_archive_phase_run AND store = $sibyl_archive_phase_store)[0];
IF $sibyl_archive_phase_current = NONE OR $sibyl_archive_phase_current.binding_json != $sibyl_archive_phase_binding_json
    OR $sibyl_archive_phase_current.binding_sha256 != $sibyl_archive_phase_binding_sha
    OR $sibyl_archive_phase_current.revision != $sibyl_archive_phase_revision
    OR $sibyl_archive_phase_current.token != $sibyl_archive_phase_token
    OR $sibyl_archive_phase_current.state = 'rolled_back'
    OR ($sibyl_archive_phase_action = 'apply' AND $sibyl_archive_phase_current.state != 'open') {
    THROW 'Archive phase admission changed';
};
FOR $candidate IN $sibyl_archive_phase_retirement_candidates {
    LET $proof = SELECT * FROM archive_phase_receipts
        WHERE organization_id = $sibyl_archive_phase_org AND actor_id = $sibyl_archive_phase_actor
            AND run_id = $sibyl_archive_phase_run AND store = $sibyl_archive_phase_store
            AND binding_sha256 = $sibyl_archive_phase_binding_sha AND action = 'apply'
            AND array::len(introduced[WHERE (kind ?? NULL) = ($candidate.kind ?? NULL) AND (destination_id ?? NULL) = ($candidate.destination_id ?? NULL) AND (physical_id ?? NULL) = ($candidate.physical_id ?? NULL) AND (row_sha256 ?? NULL) = ($candidate.row_sha256 ?? NULL) AND (body_sha256 ?? NULL) = ($candidate.body_sha256 ?? NULL) AND (audience_sha256 ?? NULL) = ($candidate.audience_sha256 ?? NULL) AND (revision ?? NULL) = ($candidate.revision ?? NULL) AND (source_incarnation ?? NULL) = ($candidate.source_incarnation ?? NULL) AND (source_generation ?? NULL) = ($candidate.source_generation ?? NULL) AND (source_state_revision ?? NULL) = ($candidate.source_state_revision ?? NULL) AND (endpoint_ids ?? NULL) = ($candidate.endpoint_ids ?? NULL) AND (endpoint_state_sha256 ?? NULL) = ($candidate.endpoint_state_sha256 ?? NULL) AND (binding_sha256 ?? NULL) = ($candidate.binding_sha256 ?? NULL)] ?? []) = 1;
    IF array::len($proof) != 1 { THROW 'Archive rollback candidate is not introduced by this run'; };
    LET $current = (SELECT * FROM type::record($candidate.physical_id))[0];
    IF $current = NONE OR crypto::sha256(type::string($current)) != $candidate.row_sha256 {
        THROW 'Archive rollback candidate changed';
    };
    IF $candidate.kind = 'graph_relationship' {
        LET $endpoints = [
            (SELECT * FROM entity WHERE id = $current.in AND group_id = $sibyl_archive_phase_org)[0],
            (SELECT * FROM entity WHERE id = $current.out AND group_id = $sibyl_archive_phase_org)[0]];
        LET $states = [
            (SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id = $sibyl_archive_phase_org AND source_kind = 'graph_entity' AND source_id = $endpoints[0].uuid)[0],
            (SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id = $sibyl_archive_phase_org AND source_kind = 'graph_entity' AND source_id = $endpoints[1].uuid)[0]];
        IF $endpoints[0] = NONE OR $endpoints[1] = NONE OR $states[0] = NONE OR $states[1] = NONE
            OR $states[0].deleted OR $states[1].deleted OR $endpoints.uuid != $candidate.endpoint_ids
            OR [crypto::sha256(type::string($states[0])), crypto::sha256(type::string($states[1]))] != $candidate.endpoint_state_sha256
            OR crypto::sha256(type::string([$current.in, $current.out, $current.source_id, $current.target_id, $current.operational_source_binding])) != $candidate.binding_sha256 {
            THROW 'Archive rollback edge endpoint or binding changed';
        };
        LET $sibyl_archive_phase_endpoint_states_to_fence = $states;
        __ARCHIVE_ENDPOINT_WRITE_WITNESS__
    } ELSE {
        LET $state = (SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id = $sibyl_archive_phase_org
            AND source_kind = $candidate.kind AND source_id = $candidate.destination_id)[0];
        IF $state = NONE OR $state.deleted OR $state.incarnation != $candidate.source_incarnation
            OR $state.generation != $candidate.source_generation OR $state.revision != $candidate.revision {
            THROW 'Archive rollback source changed';
        };
    };
};
"""
    prior_reads = """
LET $sibyl_archive_phase_prior_raw = SELECT uuid FROM raw_captures WHERE organization_id = $sibyl_archive_phase_org
    AND uuid IN ($sibyl_archive_phase_planned_creates[WHERE kind = 'raw_capture'].destination_id ?? []);
LET $sibyl_archive_phase_prior_entities = SELECT uuid FROM entity WHERE group_id = $sibyl_archive_phase_org
    AND uuid IN ($sibyl_archive_phase_planned_creates[WHERE kind = 'graph_entity'].destination_id ?? []);
LET $sibyl_archive_phase_prior_edges = SELECT uuid FROM relates_to WHERE group_id = $sibyl_archive_phase_org
    AND uuid IN ($sibyl_archive_phase_planned_creates[WHERE kind = 'graph_relationship'].destination_id ?? []);
LET $sibyl_archive_phase_prior_states = SELECT source_kind, source_id FROM source_states
    WHERE organization_id = $sibyl_archive_phase_org AND source_id IN ($sibyl_archive_phase_planned_creates.destination_id ?? []);
LET $sibyl_archive_phase_prior_associations = SELECT target_kind, target_id FROM memory_derivations
    WHERE organization_id = $sibyl_archive_phase_org AND target_id IN ($sibyl_archive_phase_planned_creates.destination_id ?? []);
LET $sibyl_archive_phase_prior_retirement_groups = SELECT VALUE retired.introduced FROM archive_phase_receipts
    WHERE organization_id = $sibyl_archive_phase_org
        AND array::len(array::intersect(retired.introduced.destination_id ?? [], $sibyl_archive_phase_planned_creates.destination_id ?? [])) > 0;
LET $sibyl_archive_phase_prior_retirements = array::flatten($sibyl_archive_phase_prior_retirement_groups);
"""
    query += _store_local_reads(prior_reads, key.store)
    query += """
FOR $planned IN $sibyl_archive_phase_planned_creates {
    LET $rows = IF $planned.kind = 'raw_capture' THEN $sibyl_archive_phase_prior_raw
        ELSE IF $planned.kind = 'graph_entity' THEN $sibyl_archive_phase_prior_entities ELSE $sibyl_archive_phase_prior_edges END;
    IF !(array::len($rows[WHERE uuid = $planned.destination_id] ?? []) = 0
            AND array::len($sibyl_archive_phase_prior_states[WHERE source_kind = $planned.kind AND source_id = $planned.destination_id] ?? []) = 0
            AND array::len($sibyl_archive_phase_prior_associations[WHERE target_kind = $planned.kind AND target_id = $planned.destination_id] ?? []) = 0
            AND array::len($sibyl_archive_phase_prior_retirements[WHERE kind = $planned.kind AND destination_id = $planned.destination_id] ?? []) = 0) {
        THROW 'Archive planned create was not absent in its declared cut';
    };
};
LET $sibyl_archive_phase_presence_cut = $sibyl_archive_phase_planned_creates;
"""
    query += writer_statements.rstrip().rstrip(";") + ";\n"
    query += _OUTCOME_GUARD_AND_COUNTS + _store_local_reads(_POST_READS, key.store)
    query += _INTRODUCED_READ + _RETIREMENT_READ
    query += """
CREATE archive_phase_receipts CONTENT {
    organization_id: $sibyl_archive_phase_org, actor_id: $sibyl_archive_phase_actor,
    run_id: $sibyl_archive_phase_run, store: $sibyl_archive_phase_store,
    binding_json: $sibyl_archive_phase_binding_json, binding_sha256: $sibyl_archive_phase_binding_sha,
    checked_plan_sha256: $sibyl_archive_phase_plan_sha,
    action: $sibyl_archive_phase_action, phase: $sibyl_archive_phase_phase,
    batch_sequence: $sibyl_archive_phase_batch, previous_revision: $sibyl_archive_phase_revision,
    committed_revision: $sibyl_archive_phase_revision + 1,
    previous_token: $sibyl_archive_phase_token, token: $sibyl_archive_phase_next_token,
    terminal: $sibyl_archive_phase_terminal, counts: $sibyl_archive_phase_counts,
    introduced: $sibyl_archive_phase_introduced, retired: $sibyl_archive_phase_retired
};
COMMIT TRANSACTION;
"""
    return PreparedArchivePhaseTransaction(
        query=render_surreal_compatible_sql(
            query.replace(
                "__ARCHIVE_ENDPOINT_WRITE_WITNESS__",
                SOURCE_STATE_WRITE_WITNESS.replace(
                    "$source_states_to_fence", "$sibyl_archive_phase_endpoint_states_to_fence"
                ).replace("$source_state", "$sibyl_archive_phase_endpoint_state"),
            ),
            url=url,
        ),
        parameters_json=parameters_json,
    )


def prepare_archive_unused_store_close(
    *,
    key: ArchivePhaseKey,
    url: str,
    expected_token: str,
    rollback_token: str,
) -> PreparedArchivePhaseTransaction:
    """Close first-apply admission without inventing a committed apply phase.

    The caller owns verified saved input and current rollback authorization.
    An unused store has no phase receipt. Native admission either closes it
    atomically with its genuine empty terminal receipt, or rejects a first
    apply winner so the caller must recover that committed phase instead.
    """
    return prepare_archive_phase_transaction(
        key=key,
        url=url,
        expected_revision=0,
        expected_token=expected_token,
        writer_statements=_UNUSED_STORE_CLOSE_WRITER,
        writer_parameters={},
        rollback_token=rollback_token,
        terminal=True,
        _unused_store_close=True,
    )


async def read_archive_phase_receipt(
    execute: SurrealExecute,
    *,
    key: ArchivePhaseKey,
    token: str,
) -> ArchivePhaseReceipt | None:
    """Read exact native evidence, rejecting closed apply even on old receipts."""
    key = ArchivePhaseKey.model_validate(key.model_dump(mode="python"))
    binding = key.binding
    ArchivePhaseControl(binding=binding, store=key.store, revision=0, token=token, state="open")
    result = await execute(
        """RETURN {
            LET $control = (SELECT * FROM archive_phase_controls
                WHERE organization_id = $org AND actor_id = $actor AND run_id = $run AND store = $store)[0];
            IF $control = NONE { RETURN NULL; };
            IF $control.binding_json != $binding_json OR $control.binding_sha256 != $binding_sha
                OR $control.token != $phase_token OR ($action = 'apply' AND $control.state != 'open') {
                THROW 'Archive phase token is closed';
            };
            RETURN (SELECT * FROM archive_phase_receipts
                WHERE organization_id = $org AND actor_id = $actor AND run_id = $run AND store = $store
                    AND checked_plan_sha256 = $plan_sha AND phase = $phase AND batch_sequence = $batch)[0];
        };""",
        org=binding.organization_id,
        actor=binding.actor_id,
        run=binding.run_id,
        store=key.store,
        binding_json=phase_binding_json(binding),
        binding_sha=binding.sha256,
        phase_token=token,
        action=key.action,
        plan_sha=binding.checked_plan_sha256,
        phase=key.phase,
        batch=key.batch_sequence,
    )
    if result is None or result == []:
        return None
    # Checked query execution returns one object; never normalize raw envelopes.
    if not isinstance(result, dict) or "binding_json" not in result:
        raise TypeError("archive phase reads require checked native object execution")
    payload = {
        "key": key.model_dump(mode="json"),
        "token": result["token"],
        "previous_token": result["previous_token"],
        "previous_revision": result["previous_revision"],
        "committed_revision": result["committed_revision"],
        "counts": result["counts"],
        "introduced": result["introduced"],
        "retired": result["retired"],
        "terminal": result["terminal"],
    }
    return ArchivePhaseReceipt.model_validate_json(strict_phase_json(payload))
