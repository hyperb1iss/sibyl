"""Conditional audit artifacts survive the actual flexible-object boundary."""

import json
from copy import deepcopy

import pytest

from sibyl_core.memory_pipeline.audit import (
    AUDIT_STORAGE_PREFIX,
    decode_audit_metadata,
    encode_audit_metadata,
)
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services import content_client
from sibyl_core.services import eval_publication as p
from sibyl_core.services.graph_records import _entity_metadata, entity_from_surreal_row
from sibyl_core.tasks import consolidation as c
from tests.test_eval_publication import admitted_pair as admitted_pair
from tests.test_eval_publication import content_store as content_store
from tests.test_eval_publication import evidence as evidence
from tests.test_eval_publication import proposal as proposal
from tests.test_eval_publication import rows
from tests.test_eval_publication_promotion import runtime as runtime


async def test_procedure_audit_actual_publication_roundtrip(proposal):
    op, result = proposal
    expected = deepcopy(result.candidate.metadata[c.METADATA_KEY])
    assert expected["build_receipt"]["openrouter_provider"] is None
    stored = await p.store_consolidation(op, result)
    assert stored.memory.metadata[c.METADATA_KEY] == expected
    record = next(row for row in await rows("raw_captures") if row["uuid"] == stored.memory.id)
    assert isinstance(record["metadata"][c.METADATA_KEY], str)
    assert json.loads(record["metadata"][c.METADATA_KEY][len(AUDIT_STORAGE_PREFIX) :]) == expected
    assert (await p.get_stored_consolidation(op)).memory.metadata[c.METADATA_KEY] == expected
    # The original flexible-object representation demonstrably loses this null.
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE raw_captures SET metadata.conditional_procedure=$audit WHERE uuid=$id;",
            audit=expected,
            id=stored.memory.id,
        )
    legacy = (await p.get_stored_consolidation(op)).memory.metadata[c.METADATA_KEY]
    assert "openrouter_provider" not in legacy["build_receipt"]


async def test_procedure_audit_graph_encoding_roundtrip(content_store):
    audit = {"receipt": {"cost": None, "requests": [{"id": None}]}, "unicode": "café"}
    entity = Entity(
        id="audit-entity",
        name="Audit",
        entity_type=EntityType.PATTERN,
        metadata={c.METADATA_KEY: audit},
    )
    metadata = _entity_metadata(entity)
    assert isinstance(metadata[c.METADATA_KEY], str)
    # JSON archive transport must retain the encoded storage value exactly.
    restored = json.loads(json.dumps(metadata))
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "DEFINE TABLE audit_graph SCHEMALESS; CREATE audit_graph CONTENT $row;",
            row={
                "uuid": entity.id,
                "name": entity.name,
                "entity_type": "pattern",
                "metadata": restored,
            },
        )
        records = content_client.normalize_records(
            await client.execute_query("SELECT * FROM audit_graph;")
        )
    assert entity_from_surreal_row(records[0]).metadata[c.METADATA_KEY] == audit


@pytest.mark.parametrize(
    "value", ["null", "[]", '{"x":NaN}', '{"x":1,"x":2}', "{", '{ "x": null }', False]
)
def test_procedure_audit_legacy_values_preserved(value):
    original = {c.METADATA_KEY: value}
    assert decode_audit_metadata(original) == original
    assert encode_audit_metadata(original) == original


@pytest.mark.parametrize(
    "encoded", ["null", "[]", '{"x":NaN}', '{"x":1,"x":2}', "{", '{ "x": null }']
)
async def test_procedure_audit_malformed_tag_preserved_but_untrusted(proposal, encoded):
    _, result = proposal
    value = AUDIT_STORAGE_PREFIX + encoded
    metadata = {c.METADATA_KEY: value}
    assert decode_audit_metadata(metadata) == metadata
    assert encode_audit_metadata(metadata) == metadata
    candidate = deepcopy(result.candidate)
    candidate.metadata[c.METADATA_KEY] = value
    assert c.validate_candidate_content_agreement(candidate, group=result.group)


def test_procedure_audit_legacy_and_no_mutation():
    original = {c.METADATA_KEY: {"nested": {"x": None}}, "ordinary": {"y": None}}
    assert decode_audit_metadata(original) == original
    encoded = encode_audit_metadata(original)
    assert decode_audit_metadata(encoded) == original
    assert original[c.METADATA_KEY]["nested"]["x"] is None
    assert encoded["ordinary"] == original["ordinary"]


async def test_procedure_audit_actual_promotion_retains_nulls(proposal, runtime):
    from sibyl_core.services.memory_reflection import promote_reflection_candidate_review
    from sibyl_core.services.surreal_content import get_raw_memory

    op, result = proposal
    expected = deepcopy(result.candidate.metadata[c.METADATA_KEY])
    stored = await p.store_consolidation(op, result)
    promoted = await promote_reflection_candidate_review(
        candidate_id=stored.memory.id,
        organization_id=op.organization_id,
        principal_id=op.principal_id,
        promote_to_scope="private",
    )
    assert promoted.success
    graph = await runtime.entity_manager.get(promoted.promoted_id)
    raw = await get_raw_memory(organization_id=op.organization_id, memory_id=stored.memory.id)
    assert graph.metadata[c.METADATA_KEY] == expected
    assert raw.metadata[c.METADATA_KEY] == expected
    again = await promote_reflection_candidate_review(
        candidate_id=stored.memory.id,
        organization_id=op.organization_id,
        principal_id=op.principal_id,
        promote_to_scope="private",
    )
    assert again.success and again.promoted_id == promoted.promoted_id


@pytest.mark.parametrize(
    "value", ["historical user annotation", '{"x":null}', False, AUDIT_STORAGE_PREFIX + "{"]
)
async def test_procedure_audit_legacy_does_not_poison_scope_list(content_store, value):
    from sibyl_core.services.content_raw_persistence import remember_raw_memory
    from sibyl_core.services.content_raw_recall import list_raw_memories_for_scope

    ordinary = await remember_raw_memory(
        organization_id="org",
        principal_id="owner",
        source_id="ordinary",
        raw_content="ordinary preserved",
        embedding_provider=None,
    )
    legacy = await remember_raw_memory(
        organization_id="org",
        principal_id="owner",
        source_id="legacy",
        raw_content="historical metadata",
        embedding_provider=None,
    )
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE raw_captures SET metadata.conditional_procedure=$value WHERE uuid=$uuid;",
            value=value,
            uuid=legacy.id,
        )
    memories = await list_raw_memories_for_scope(
        organization_id="org", principal_id="owner", include_lifecycle_hidden=True
    )
    by_id = {m.id: m for m in memories}
    assert ordinary.id in by_id
    assert by_id[legacy.id].metadata[c.METADATA_KEY] == value


@pytest.mark.parametrize(
    "value",
    [
        AUDIT_STORAGE_PREFIX + "{",
        AUDIT_STORAGE_PREFIX + "{}",
        AUDIT_STORAGE_PREFIX + '{"x":1e9999}',
    ],
)
async def test_procedure_audit_invalid_artifact_never_writes_graph(proposal, runtime, value):
    from sibyl_core.services.memory_reflection import promote_reflection_candidate_review

    op, result = proposal
    stored = await p.store_consolidation(op, result)
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE raw_captures SET metadata.conditional_procedure=$value WHERE uuid=$uuid;",
            value=value,
            uuid=stored.memory.id,
        )
    promoted = await promote_reflection_candidate_review(
        candidate_id=stored.memory.id,
        organization_id=op.organization_id,
        principal_id=op.principal_id,
        promote_to_scope="private",
    )
    assert not promoted.success
    assert await runtime.entity_manager.list_by_type(EntityType.PROCEDURE) == []


def test_procedure_audit_deep_invalid_tag_remains_opaque():
    value = AUDIT_STORAGE_PREFIX + "[" * 2000 + "0" + "]" * 2000
    assert decode_audit_metadata({c.METADATA_KEY: value}) == {c.METADATA_KEY: value}


@pytest.mark.parametrize("tamper", [None, "body", "receipt", "source", "metadata"])
async def test_procedure_audit_legacy_recovery_requires_complete_agreement(proposal, tamper):
    from dataclasses import replace

    from sibyl_core.services.procedure_artifact import resolve_procedure_artifact

    op, result = proposal
    receipt = deepcopy(result.receipt)
    receipt.pop("render_version")
    legacy = c._candidate(
        result.group,
        result.proposal.procedure,
        receipt,
        c._extraction_input(result.group),
        render_version=None,
    )
    stored = await p.store_consolidation(op, replace(result, candidate=legacy, receipt=receipt))
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE raw_captures SET metadata.conditional_procedure=$audit WHERE uuid=$uuid;",
            audit=legacy.metadata[c.METADATA_KEY],
            uuid=stored.memory.id,
        )
    memory = (await p.get_stored_consolidation(op)).memory
    ledger = (await rows("eval_consolidations"))[0]
    captures = await rows("raw_captures")
    if tamper == "body":
        memory = replace(
            memory,
            raw_content=memory.raw_content.replace(
                '"schema_version":', '"schema_version":"forged","schema_version":', 1
            ),
        )
    elif tamper == "receipt":
        changed = json.loads(ledger["build_receipt_json"])
        changed["output_sha256"] = "0" * 64
        ledger["build_receipt_json"] = json.dumps(
            changed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    elif tamper == "source":
        source_id = result.group.episodes[0].stored_sources[0].source_id
        next(row for row in captures if row["uuid"] == source_id)["raw_content"] = "replaced"
    elif tamper == "metadata":
        memory.metadata[c.METADATA_KEY]["procedure"]["goal"]["statement"] = "replaced"
    artifact = resolve_procedure_artifact(memory, ledger, captures)
    assert (artifact is not None) == (tamper is None)
    if artifact is not None:
        assert artifact.candidate.content == legacy.content
        assert artifact.candidate.metadata[c.METADATA_KEY] == legacy.metadata[c.METADATA_KEY]


async def test_procedure_audit_corruption_after_promotion_denies_recall(proposal, runtime):
    from sibyl_core.services.content_raw_recall import recall_raw_memory
    from sibyl_core.services.memory_reflection import promote_reflection_candidate_review

    op, result = proposal
    stored = await p.store_consolidation(op, result)
    promoted = await promote_reflection_candidate_review(
        candidate_id=stored.memory.id,
        organization_id=op.organization_id,
        principal_id=op.principal_id,
        promote_to_scope="private",
    )
    assert promoted.success
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE raw_captures SET metadata.conditional_procedure=$value WHERE uuid=$uuid;",
            value=AUDIT_STORAGE_PREFIX + "{}",
            uuid=stored.memory.id,
        )
    memories = await recall_raw_memory(
        organization_id=op.organization_id,
        principal_id=op.principal_id,
        query="Check the actual output",
    )
    assert stored.memory.id not in {memory.id for memory in memories}


async def test_procedure_audit_verification_builds_once_off_event_loop(proposal, monkeypatch):
    import threading

    from sibyl_core.services.eval_publication_guards import verify_publication_admissions

    op, result = proposal
    stored = await p.store_consolidation(op, result)
    main_thread = threading.get_ident()
    calls = []
    original = c._extraction_input

    def observed(group):
        calls.append(threading.get_ident())
        return original(group)

    monkeypatch.setattr(c, "_extraction_input", observed)
    assert await verify_publication_admissions(stored.memory)
    assert len(calls) == 1
    assert calls[0] != main_thread


async def test_procedure_audit_receipt_change_at_commit_denies_promotion(
    proposal, runtime, monkeypatch
):
    from sibyl_core.services.memory_reflection import promote_reflection_candidate_review

    operation, result = proposal
    stored = await p.store_consolidation(operation, result)
    mutations = []
    async with content_client.surreal_content_client() as client:
        execute = client.execute_query_raw

        async def intercepted(sql, **params):
            if (
                params.get("publication_operation_id")
                and params.get("source_observations")
                and not mutations
            ):
                await client.execute_query(
                    "UPDATE eval_consolidations SET build_receipt_json=$value WHERE uuid=$uuid;",
                    value="{}",
                    uuid=params["publication_operation_id"],
                )
                mutations.append(True)
            return await execute(sql, **params)

        monkeypatch.setattr(client, "execute_query_raw", intercepted)
        outcome = await promote_reflection_candidate_review(
            candidate_id=stored.memory.id,
            organization_id=operation.organization_id,
            principal_id=operation.principal_id,
            promote_to_scope="private",
        )
    assert mutations == [True]
    assert not outcome.success
