"""Distilled notes retain the current source and reject unbound collisions."""

import pytest

from sibyl_core.ai.operational_distillation import (
    DistilledOperationalNotes,
    build_operational_note_entities_with_receipt,
)
from sibyl_core.services.operational_notes import publish_operational_notes
from sibyl_core.services.source_observations import SourceUnavailableError
from tests.test_operational_projection import authority as authority
from tests.test_operational_projection import capture
from tests.test_operational_projection import content_store as content_store
from tests.test_operational_projection import runtime as runtime


async def test_notes_publish_with_source_binding_and_replay(runtime, content_store, authority):
    _, source = await capture(runtime, authority)
    notes = DistilledOperationalNotes(workflow="Read the project state before writing.")
    first, _ = await publish_operational_notes(
        runtime.entity_manager, source, notes, provider=None, model=None, profile="baseline"
    )
    assert first
    before = await runtime.client.execute_query("SELECT * FROM entity ORDER BY uuid;")
    second, _ = await publish_operational_notes(
        runtime.entity_manager, source, notes, provider=None, model=None, profile="baseline"
    )
    assert [e.id for e in first] == [e.id for e in second]
    assert await runtime.client.execute_query("SELECT * FROM entity ORDER BY uuid;") == before
    rows = await runtime.client.execute_query("SELECT * FROM memory_derivations;")
    assert len(rows) == len(first)
    assert all(
        row["observations"][0]["source"]["id"] == source.observation.source.id for row in rows
    )


async def test_notes_refuse_unbound_existing_target(runtime, content_store, authority):
    _, source = await capture(runtime, authority)
    notes = DistilledOperationalNotes(workflow="Read before writing.")
    _, experience = await source.current()
    projection = await source.projection()
    published, _ = build_operational_note_entities_with_receipt(
        notes,
        experience=experience,
        organization_id=runtime.client.group_id,
        created_by=source.creator_id,
        content_hash=projection.manifest.content_hash,
    )
    await runtime.entity_manager.create_direct_bulk(published, generate_embeddings=False)
    with pytest.raises(SourceUnavailableError):
        await publish_operational_notes(
            runtime.entity_manager, source, notes, provider=None, model=None, profile="baseline"
        )
    assert published


async def test_notes_remove_only_omitted_bound_note_inventory(runtime, content_store, authority):
    _, source = await capture(runtime, authority)
    first, _ = await publish_operational_notes(
        runtime.entity_manager,
        source,
        DistilledOperationalNotes(workflow="Read before writing."),
        provider=None,
        model=None,
        profile="baseline",
    )
    second, receipt = await publish_operational_notes(
        runtime.entity_manager,
        source,
        DistilledOperationalNotes(facts=["Project uses an index."]),
        provider=None,
        model=None,
        profile="baseline",
    )
    assert receipt["retired_note_ids"] == [first[0].id]
    from sibyl_core.services.graph_derivations import graph_association_current

    retained = await runtime.entity_manager.get(first[0].id)
    binding = (
        await runtime.client.execute_query(
            "SELECT * FROM memory_derivations WHERE target_id=$id;", id=first[0].id
        )
    )[0]
    assert binding["active"] is False
    assert not await graph_association_current(retained, binding)
    assert (await runtime.entity_manager.get(second[0].id)).derivation_required


@pytest.mark.parametrize("hard_delete", [False, True])
async def test_omitted_note_regeneration_preserves_hard_deletion(
    runtime, content_store, authority, hard_delete
):
    _, source = await capture(runtime, authority)
    notes = DistilledOperationalNotes(workflow="Read before writing.")
    first, _ = await publish_operational_notes(
        runtime.entity_manager, source, notes, provider=None, model=None, profile="baseline"
    )
    await publish_operational_notes(
        runtime.entity_manager,
        source,
        DistilledOperationalNotes(facts=["Index present."]),
        provider=None,
        model=None,
        profile="baseline",
    )
    if hard_delete:
        assert await runtime.entity_manager.delete(first[0].id)
        binding = (
            await runtime.client.execute_query(
                "SELECT * FROM memory_derivations WHERE target_id=$id;", id=first[0].id
            )
        )[0]
        assert binding.get("operational_omission") is None
        with pytest.raises(SourceUnavailableError):
            await publish_operational_notes(
                runtime.entity_manager, source, notes, provider=None, model=None, profile="baseline"
            )
    else:
        await publish_operational_notes(
            runtime.entity_manager, source, notes, provider=None, model=None, profile="baseline"
        )
        binding = (
            await runtime.client.execute_query(
                "SELECT * FROM memory_derivations WHERE target_id=$id;", id=first[0].id
            )
        )[0]
        assert binding["active"] is True
        assert binding.get("operational_omission") is None


async def test_omission_archive_cannot_restore_over_physical_delete(
    runtime, content_store, authority
):
    from sibyl_core.memory_pipeline.observations import SourceKind
    from sibyl_core.services.source_archive_store import (
        export_source_integrity,
        restore_source_integrity,
    )

    _, source = await capture(runtime, authority)
    notes = DistilledOperationalNotes(workflow="Read before writing.")
    first, _ = await publish_operational_notes(
        runtime.entity_manager, source, notes, provider=None, model=None, profile="baseline"
    )
    await publish_operational_notes(
        runtime.entity_manager,
        source,
        DistilledOperationalNotes(facts=["Index present."]),
        provider=None,
        model=None,
        profile="baseline",
    )
    archive = await export_source_integrity(
        runtime.client.execute_query,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
    )
    await runtime.entity_manager.delete(first[0].id)
    result = await restore_source_integrity(
        runtime.client.execute_query,
        archive,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
        skip_existing=False,
    )
    assert result["conflicts"]
    with pytest.raises(KeyError):
        await runtime.entity_manager.get(first[0].id)
    with pytest.raises(SourceUnavailableError):
        await publish_operational_notes(
            runtime.entity_manager, source, notes, provider=None, model=None, profile="baseline"
        )


async def test_omission_source_update_rejects_old_job(runtime, content_store, authority):
    memory, old = await capture(runtime, authority)
    notes = DistilledOperationalNotes(workflow="Read before writing.")
    await publish_operational_notes(
        runtime.entity_manager, old, notes, provider=None, model=None, profile="baseline"
    )
    await publish_operational_notes(
        runtime.entity_manager,
        old,
        DistilledOperationalNotes(facts=["Index present."]),
        provider=None,
        model=None,
        profile="baseline",
    )
    _, current = await capture(runtime, authority, outcome="unknown", revision=memory.revision)
    with pytest.raises(SourceUnavailableError):
        await publish_operational_notes(
            runtime.entity_manager, old, notes, provider=None, model=None, profile="baseline"
        )
    assert (
        await publish_operational_notes(
            runtime.entity_manager, current, notes, provider=None, model=None, profile="baseline"
        )
    )[0]


@pytest.mark.parametrize("change", ["purge", "incarnation"])
async def test_omission_never_grants_source_reincarnation(
    runtime, content_store, authority, change
):
    from sibyl_core.services.content_client import surreal_content_client
    from sibyl_core.services.operational_projection import load_operational_projection_source

    _, old = await capture(runtime, authority)
    notes = DistilledOperationalNotes(workflow="Read before writing.")
    await publish_operational_notes(
        runtime.entity_manager, old, notes, provider=None, model=None, profile="baseline"
    )
    await publish_operational_notes(
        runtime.entity_manager,
        old,
        DistilledOperationalNotes(facts=["Index present."]),
        provider=None,
        model=None,
        profile="baseline",
    )
    async with surreal_content_client() as client:
        if change == "purge":
            await client.execute_query(
                "DELETE raw_captures WHERE uuid=$id;", id=old.observation.source.id
            )
        else:
            await client.execute_query(
                "UPDATE source_states SET incarnation='new-incarnation',generation+=1 WHERE source_id=$id;",
                id=old.observation.source.id,
            )
    with pytest.raises(SourceUnavailableError):
        await publish_operational_notes(
            runtime.entity_manager, old, notes, provider=None, model=None, profile="baseline"
        )
    if change == "incarnation":
        current = await load_operational_projection_source(old.observation.source, authority)
        with pytest.raises(SourceUnavailableError):
            await publish_operational_notes(
                runtime.entity_manager,
                current,
                notes,
                provider=None,
                model=None,
                profile="baseline",
            )


@pytest.mark.parametrize("field", ["version", "target_id", "authority_ceiling", "source"])
async def test_omission_receipt_rejects_malformed_binding(runtime, content_store, authority, field):
    from copy import deepcopy

    from sibyl_core.services.operational_omission import validate_omission_receipt

    _, source = await capture(runtime, authority)
    notes, _ = await publish_operational_notes(
        runtime.entity_manager,
        source,
        DistilledOperationalNotes(workflow="Inspect first."),
        provider=None,
        model=None,
        profile="baseline",
    )
    await publish_operational_notes(
        runtime.entity_manager,
        source,
        DistilledOperationalNotes(facts=["Index present."]),
        provider=None,
        model=None,
        profile="baseline",
    )
    binding = (
        await runtime.client.execute_query(
            "SELECT * FROM memory_derivations WHERE target_id=$id;", id=notes[0].id
        )
    )[0]
    assert validate_omission_receipt(binding) is not None
    changed = deepcopy(binding)
    changed["operational_omission"][field] = True if field == "version" else "malformed"
    with pytest.raises(ValueError, match="omission"):
        validate_omission_receipt(changed)
