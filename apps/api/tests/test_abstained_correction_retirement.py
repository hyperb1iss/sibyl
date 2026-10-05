"""Abstained correction chains retire without orphaning retained drafts."""

from unittest.mock import AsyncMock

import pytest

from sibyl.jobs import reflection
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.reflection_validation import prepare_stored_reflection
from sibyl_core.services.surreal_content import remember_raw_memory
from sibyl_core.tasks.memory_progress import ProgressCriticOutput
from tests.test_automatic_dream_validation import ordinary_graph as ordinary_graph  # noqa: PLC0414
from tests.test_dream_source_checkpoints import dream_store as dream_store  # noqa: PLC0414


@pytest.fixture
async def corrected_chain(dream_store, monkeypatch):
    import json
    from itertools import cycle

    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from sibyl_core.ai.llm.extractor import Extractor
    from sibyl_core.services import procedure_validation
    from sibyl_core.services.automatic_reflection import _persist_corrected
    from sibyl_core.services.reflection_validation import validate_reflection_stage
    from sibyl_core.tasks.memory_validation import CriticOutput
    from sibyl_core.tasks.procedure_review import ReviewSubmission, review_digest

    await remember_raw_memory(
        organization_id="dream-org",
        principal_id="owner",
        source_id="session",
        raw_content="Decision: validate inputs before parsing.",
        embedding_provider=None,
    )
    await reflection.run_reflection_dream_cycle({}, "dream-org", candidate_limit=0)
    rows = await dream_store.execute_query(
        "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate';"
    )
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    parent = await prepare_stored_reflection("dream-org", "owner", rows[0]["uuid"], resolver)
    assertion = json.loads(parent.prepared.payload_json)["assertions"]["/content"]
    review = ReviewSubmission(
        parent_operation_id=json.loads(parent.prepared.payload_json)["parent_operation_id"],
        parent_candidate_sha256=json.loads(parent.prepared.payload_json)["parent_candidate_sha256"],
        findings=[
            {
                "claim_path": "/content",
                "claim_sha256": review_digest(assertion),
                "evidence_refs": [{"evidence_id": "s0"}],
                "basis": "factual_contradiction",
                "disposition": "reconsider",
                "critique": "Preserve the observed ordering.",
            }
        ],
    )
    output = {
        "content": "Validate inputs before parsing.",
        "abstention_reason": None,
        "assessments": [
            {
                "finding_id": review.finding_ids()[0],
                "disposition": "accepted",
                "explanation": "The original decision specifies ordering.",
                "evidence_refs": [{"evidence_id": "s0"}],
            }
        ],
    }
    extractor = Extractor(
        CriticOutput, agent=Agent(TestModel(custom_output_args=output), output_type=CriticOutput)
    )
    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(return_value=(extractor, '{"max_input_chars":40000,"model":"offline"}')),
    )
    outcome = await validate_reflection_stage(parent, resolver, review)
    assert outcome["status"] == "corrected"

    first = await _persist_corrected(parent, resolver, outcome)
    replay = await _persist_corrected(parent, resolver, outcome)
    assert first.id == replay.id
    assert first.revision == replay.revision
    child = await prepare_stored_reflection("dream-org", "owner", first.id, resolver)
    assert child.candidate.content == output["content"]
    critic = Extractor(
        CriticOutput,
        agent=Agent(
            TestModel(
                custom_output_args={"findings": [review.findings[0].model_dump(mode="json")]}
            ),
            output_type=CriticOutput,
        ),
    )
    recheck = Extractor(
        ProgressCriticOutput,
        agent=Agent(
            TestModel(
                custom_output_args={
                    "findings": [],
                    "abstention_reason": "Evidence supports no further useful correction.",
                    "prior_assessments": [
                        {
                            "finding_id": review.finding_ids()[0],
                            "disposition": "resolved",
                            "supported_reduction": "The corrected ordering agrees with the original decision.",
                            "remaining_concern": None,
                            "evidence_refs": [{"evidence_id": "s0"}],
                        }
                    ],
                }
            ),
            output_type=ProgressCriticOutput,
        ),
    )
    monkeypatch.setattr(
        procedure_validation,
        "_validation_extractor",
        AsyncMock(return_value=(recheck, '{"max_input_chars":40000,"model":"offline"}')),
    )
    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(
            side_effect=cycle(
                [
                    (critic, '{"max_input_chars":40000,"model":"offline"}'),
                    (extractor, '{"max_input_chars":40000,"model":"offline"}'),
                ]
            )
        ),
    )
    return parent, resolver, first, extractor, review


@pytest.mark.parametrize(
    "case",
    [
        "live",
        "drain",
        "partial",
        "historical",
        "revoked",
        "source_changed",
        "missing_origin",
        "metadata_ignored",
        "promoted",
        "concurrent_child",
        "concurrent_parent",
        "concurrent_source",
    ],
)
async def test_abstained_correction_chain_retires(corrected_chain, monkeypatch, case):
    parent, resolver, first, _, _ = corrected_chain
    from dataclasses import replace

    from sibyl_core.services import procedure_validation, reflection_retirement
    from sibyl_core.services.automatic_reflection import automatically_review_reflection
    from sibyl_core.services.content_raw_persistence import save_raw_memory
    from sibyl_core.services.source_observations import SourceUnavailableError
    from sibyl_core.services.surreal_content import get_raw_memory

    if case not in {"live", "drain", "partial"}:
        # A retained database can contain rows archived by the old implementation.
        await save_raw_memory(
            replace(
                parent.memory,
                review_state="archived",
                metadata={
                    **parent.memory.metadata,
                    "review_state": "archived",
                    "autonomy_outcome": "abstained",
                    "archive_reason": "legacy abstention",
                },
            ),
            expected_revision=parent.memory.revision,
        )

    first = await _change_retirement_case(case, parent, first, resolver, monkeypatch)

    procedure_validation.validation_extractor.reset_mock()
    procedure_validation._validation_extractor.reset_mock()

    async def resume():
        return await automatically_review_reflection(
            "dream-org",
            "owner",
            parent.memory.id if case in {"live", "partial"} else first.id,
            resolver,
        )

    before = await reflection_retirement._query(
        "SELECT * FROM raw_captures WHERE organization_id='dream-org' ORDER BY uuid;"
    )
    if case == "partial":
        from pydantic_ai.models.test import TestModel

        original_record = reflection_retirement.record_abstained_reflection
        writes = 0

        async def interrupt_after_root(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise RuntimeError("retirement interrupted")
            await original_record(*args, **kwargs)

        monkeypatch.setattr(
            reflection_retirement, "record_abstained_reflection", interrupt_after_root
        )
        with pytest.raises(RuntimeError, match="retirement interrupted"):
            await resume()
        monkeypatch.setattr(reflection_retirement, "record_abstained_reflection", original_record)
        monkeypatch.setattr(
            TestModel, "request", AsyncMock(side_effect=AssertionError("model redispatch"))
        )

    if case in {"revoked", "source_changed", "missing_origin", "promoted"} or case.startswith(
        "concurrent_"
    ):
        with pytest.raises(SourceUnavailableError):
            await resume()
    elif case == "drain":
        drained = await reflection.run_reflection_dream_cycle(
            {}, "dream-org", source_limit=0, candidate_limit=50
        )
        assert drained["failed"] == 0
        assert drained["archived"] == 1
        assert drained["candidates"][0]["review_state"] == "archived"
        assert drained["candidates"][0]["raw_review_state"] == "pending"
    else:
        resumed = await resume()
        assert resumed.status == "abstained"
    if case not in {"live", "drain", "partial"}:
        procedure_validation.validation_extractor.assert_not_called()
        procedure_validation._validation_extractor.assert_not_called()
    if case not in {"live", "drain", "partial", "historical", "metadata_ignored"}:
        current = await get_raw_memory(organization_id="dream-org", memory_id=first.id)
        assert current is not None
        assert current.review_state == ("promoted" if case == "promoted" else "pending")
        return
    after = await reflection_retirement._query(
        "SELECT * FROM raw_captures WHERE organization_id='dream-org' ORDER BY uuid;"
    )
    assert after == before
    terminal = await reflection_retirement._query(
        "SELECT * FROM reflection_supersessions WHERE organization_id='dream-org';"
    )
    assert first.id in {row["draft_id"] for row in terminal}
    assert {row["archive_reason"] for row in terminal} <= {"abstained", "ancestor_abstained"}
    repeat = await reflection.run_reflection_dream_cycle(
        {}, "dream-org", source_limit=0, candidate_limit=50
    )
    assert repeat["failed"] == 0
    assert repeat["candidates_scanned"] == 0


@pytest.mark.parametrize("record_promotion", [False, True])
async def test_abstention_retirement_preserves_published_descendant_evidence(
    dream_store, monkeypatch, record_promotion
):
    from dataclasses import replace

    from sibyl_core.services.automatic_reflection import automatically_review_reflection
    from sibyl_core.services.content_raw_persistence import save_raw_memory
    from sibyl_core.services.memory_reflection import promote_reflection_candidate_review
    from sibyl_core.services.ordinary_publication import ordinary_promotion_binding
    from sibyl_core.services.reflection_supersession import (
        correction_parent_id,
        retire_superseded_reflection_drafts,
    )
    from tests.test_automatic_dream_validation import _install_repair_rig

    await remember_raw_memory(
        organization_id="dream-org",
        principal_id="owner",
        source_id="session",
        raw_content="Decision: validate inputs before parsing and retain the original bytes.",
        embedding_provider=None,
    )
    await reflection.run_reflection_dream_cycle({}, "dream-org", candidate_limit=0)
    rows = await dream_store.execute_query(
        "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate';"
    )
    root_id = rows[0]["uuid"]
    resolver, _, _ = _install_repair_rig(monkeypatch)
    reviewed = await automatically_review_reflection("dream-org", "owner", root_id, resolver)
    assert reviewed.status == "corrected"
    assert reviewed.candidate is not None
    binding = await ordinary_promotion_binding(
        "dream-org", "owner", reviewed.candidate.id, reviewed.executions[-1], resolver, AsyncMock()
    )
    with monkeypatch.context() as promotion_window:
        if not record_promotion:
            from sibyl_core.services import reflection_supersession

            promotion_window.setattr(
                reflection_supersession,
                "retire_superseded_reflection_drafts",
                AsyncMock(side_effect=RuntimeError("crash after publication")),
            )
        promoted = await promote_reflection_candidate_review(
            organization_id="dream-org",
            principal_id="owner",
            candidate_id=reviewed.candidate.id,
            expected_candidate_revision=reviewed.candidate.revision,
            promote_to_scope="private",
            validation_promotion=binding,
        )
    assert promoted.success
    middle_id = correction_parent_id(reviewed.candidate)
    assert middle_id is not None
    assert middle_id != root_id
    if record_promotion:
        await retire_superseded_reflection_drafts(
            organization_id="dream-org", promoted_candidate_id=reviewed.candidate.id
        )
    if record_promotion:
        from sibyl_core.services.memory_reflection import preview_reflection_candidate_promotion

        preview = await preview_reflection_candidate_promotion(
            organization_id="dream-org",
            principal_id="owner",
            candidate_id=reviewed.candidate.id,
            promote_to_scope="private",
        )
        assert preview.allowed
        replay = await promote_reflection_candidate_review(
            organization_id="dream-org",
            principal_id="owner",
            candidate_id=reviewed.candidate.id,
            promote_to_scope="private",
        )
        assert replay.success
    prior_records = await dream_store.execute_query(
        "SELECT * FROM reflection_supersessions ORDER BY draft_id;"
    )
    root = await prepare_stored_reflection("dream-org", "owner", root_id, resolver)
    await save_raw_memory(
        replace(
            root.memory,
            review_state="archived",
            metadata={
                **root.memory.metadata,
                "review_state": "archived",
                "autonomy_outcome": "abstained",
            },
        ),
        expected_revision=root.memory.revision,
    )
    before = await dream_store.execute_query("SELECT * FROM raw_captures ORDER BY uuid;")
    await automatically_review_reflection("dream-org", "owner", middle_id, resolver)
    assert await dream_store.execute_query("SELECT * FROM raw_captures ORDER BY uuid;") == before
    after_records = await dream_store.execute_query(
        "SELECT * FROM reflection_supersessions ORDER BY draft_id;"
    )
    if record_promotion:
        assert after_records == prior_records
    else:
        assert len(after_records) == 1
        assert after_records[0]["draft_id"] == middle_id
        assert after_records[0]["archive_reason"] == "ancestor_abstained"


async def _change_retirement_case(case, parent, first, resolver, monkeypatch):
    from dataclasses import replace

    from sibyl_core.services import reflection_retirement
    from sibyl_core.services.content_raw_persistence import get_raw_memory, save_raw_memory

    if case == "revoked":
        resolver.return_value = None
    elif case == "source_changed":
        source = parent.sources[0]
        await save_raw_memory(
            replace(source, raw_content="New source decision."), expected_revision=source.revision
        )
    elif case == "missing_origin":
        monkeypatch.setattr(
            reflection_retirement, "load_raw_derivation", AsyncMock(return_value=None)
        )
    elif case == "metadata_ignored":
        first = await save_raw_memory(
            replace(
                first, metadata={**first.metadata, "automatic_correction": {"parent_id": "forged"}}
            ),
            expected_revision=first.revision,
        )
    elif case == "promoted":
        first = await save_raw_memory(
            replace(first, review_state="promoted"), expected_revision=first.revision
        )
    elif case.startswith("concurrent_"):
        original_query = reflection_retirement._query

        async def race(query, **kwargs):
            if "CREATE reflection_supersessions" not in query:
                return await original_query(query, **kwargs)
            target_id = {
                "concurrent_child": first.id,
                "concurrent_parent": parent.memory.id,
                "concurrent_source": parent.sources[0].id,
            }[case]
            target = await get_raw_memory(organization_id="dream-org", memory_id=target_id)
            assert target is not None
            await save_raw_memory(
                replace(target, title="Concurrent edit"), expected_revision=target.revision
            )
            return await original_query(query, **kwargs)

        monkeypatch.setattr(reflection_retirement, "_query", race)

    return first


async def test_terminal_root_retires_unselected_protected_sibling(corrected_chain, monkeypatch):
    from sibyl_core.services import procedure_validation
    from sibyl_core.services.automatic_reflection import (
        _persist_corrected,
        automatically_review_reflection,
    )
    from sibyl_core.services.reflection_retirement import _query
    from sibyl_core.services.reflection_validation import validate_reflection_stage

    parent, resolver, selected, extractor, review = corrected_chain
    with monkeypatch.context() as sibling_model:
        sibling_model.setattr(
            procedure_validation,
            "validation_extractor",
            AsyncMock(
                return_value=(extractor, '{"max_input_chars":40000,"model":"offline-sibling"}')
            ),
        )
        outcome = await validate_reflection_stage(parent, resolver, review)
        sibling = await _persist_corrected(parent, resolver, outcome)
    assert sibling.id != selected.id
    before = await _query("SELECT * FROM raw_captures ORDER BY uuid;")
    first = await automatically_review_reflection("dream-org", "owner", parent.memory.id, resolver)
    assert first.status == "abstained"
    no_dispatch = AsyncMock(side_effect=AssertionError("terminal root must not dispatch"))
    monkeypatch.setattr(procedure_validation, "validation_extractor", no_dispatch)
    monkeypatch.setattr(procedure_validation, "_validation_extractor", no_dispatch)
    resumed = await automatically_review_reflection("dream-org", "owner", sibling.id, resolver)
    assert resumed.status == "abstained"
    assert sibling.id in resumed.candidate_ids
    assert await _query("SELECT * FROM raw_captures ORDER BY uuid;") == before
    terminal = await _query(
        "SELECT * FROM reflection_supersessions WHERE draft_id=$id;", id=sibling.id
    )
    assert terminal[0]["archive_reason"] == "ancestor_abstained"
    no_dispatch.assert_not_called()


async def test_terminal_frontier_cannot_redirect_to_unrelated_root(corrected_chain, monkeypatch):
    from sibyl_core.services import procedure_validation
    from sibyl_core.services.automatic_reflection import automatically_review_reflection
    from sibyl_core.services.reflection_retirement import _query
    from sibyl_core.services.source_observations import SourceUnavailableError

    parent, resolver, selected, _, _ = corrected_chain
    await automatically_review_reflection("dream-org", "owner", parent.memory.id, resolver)
    await remember_raw_memory(
        organization_id="dream-org",
        principal_id="owner",
        source_id="other-session",
        raw_content="Decision: retain request identifiers through retries.",
        embedding_provider=None,
    )
    await reflection.run_reflection_dream_cycle({}, "dream-org", candidate_limit=0)
    roots = await _query(
        "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate' "
        "AND uuid NOT IN $ids;",
        ids=[parent.memory.id, selected.id],
    )
    assert len(roots) == 1
    await _query(
        "UPDATE reflection_supersessions SET superseded_by_candidate_id=$frontier "
        "WHERE organization_id='dream-org' AND draft_id=$root;",
        frontier=roots[0]["uuid"],
        root=parent.memory.id,
    )
    before = await _query("SELECT * FROM raw_captures ORDER BY uuid;")
    no_dispatch = AsyncMock(side_effect=AssertionError("invalid frontier must not dispatch"))
    monkeypatch.setattr(procedure_validation, "validation_extractor", no_dispatch)
    with pytest.raises(SourceUnavailableError):
        await automatically_review_reflection("dream-org", "owner", parent.memory.id, resolver)
    assert await _query("SELECT * FROM raw_captures ORDER BY uuid;") == before
    no_dispatch.assert_not_called()


@pytest.mark.parametrize("target", ["root", "child"])
@pytest.mark.parametrize("entrypoint", ["preview", "promote"])
async def test_abstained_ledger_blocks_manual_promotion(corrected_chain, target, entrypoint):
    from sibyl_core.services.automatic_reflection import automatically_review_reflection
    from sibyl_core.services.memory_reflection import (
        preview_reflection_candidate_promotion,
        promote_reflection_candidate_review,
    )
    from sibyl_core.services.reflection_retirement import _query

    parent, resolver, child, _, _ = corrected_chain
    await automatically_review_reflection("dream-org", "owner", parent.memory.id, resolver)
    before = await _query("SELECT * FROM raw_captures ORDER BY uuid;")
    candidate_id = parent.memory.id if target == "root" else child.id
    operation = (
        preview_reflection_candidate_promotion
        if entrypoint == "preview"
        else promote_reflection_candidate_review
    )
    result = await operation(
        organization_id="dream-org",
        principal_id="owner",
        candidate_id=candidate_id,
        promote_to_scope="private",
    )
    assert not (result.allowed if entrypoint == "preview" else result.success)
    assert result.reason == "candidate_archived"
    assert result.review_state == "archived"
    assert await _query("SELECT * FROM raw_captures ORDER BY uuid;") == before


@pytest.mark.parametrize(
    "phase",
    ["reservation", "graph_entry", "graph_created", "final_state", "graph_crash", "final_crash"],
)
async def test_terminalization_wins_inflight_promotion(
    corrected_chain, monkeypatch, ordinary_graph, phase
):
    from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
    from sibyl_core.services import memory_reflection
    from sibyl_core.services.automatic_reflection import _abstain
    from sibyl_core.services.reflection_retirement import _query

    parent, resolver, _, _, _ = corrected_chain
    terminal_cut = []

    async def terminalize():
        current = await prepare_stored_reflection("dream-org", "owner", parent.memory.id, resolver)
        await _abstain(current, resolver, parent.memory.id)
        terminal_cut.extend(await _query("SELECT * FROM raw_captures ORDER BY uuid;"))

    save = memory_reflection.save_raw_memory
    persist = memory_reflection.persist_reflection_candidate
    create = ordinary_graph.entity_manager.create_direct_if_absent

    async def racing_save(memory, **kwargs):
        if (phase == "reservation" and not terminal_cut) or (
            phase in {"final_state", "final_crash"} and memory.review_state == "promoted"
        ):
            await terminalize()
            if phase == "final_crash":
                raise RuntimeError("publication crash")
        return await save(memory, **kwargs)

    async def racing_persist(**kwargs):
        if phase == "graph_entry":
            await terminalize()
        return await persist(**kwargs)

    async def racing_create(*args, **kwargs):
        result = await create(*args, **kwargs)
        if phase in {"graph_created", "graph_crash"}:
            await terminalize()
            if phase == "graph_crash":
                raise RuntimeError("publication crash")
        return result

    monkeypatch.setattr(memory_reflection, "save_raw_memory", racing_save)
    monkeypatch.setattr(memory_reflection, "persist_reflection_candidate", racing_persist)
    monkeypatch.setattr(ordinary_graph.entity_manager, "create_direct_if_absent", racing_create)

    async def promote():
        return await memory_reflection.promote_reflection_candidate_review(
            organization_id="dream-org",
            principal_id="owner",
            candidate_id=parent.memory.id,
            promote_to_scope="private",
        )

    if phase in {"graph_crash", "final_crash"}:
        with pytest.raises(RuntimeError, match="publication crash"):
            await promote()
    else:
        result = await promote()
        assert not result.success
        assert result.reason == "candidate_archived"
        assert result.review_state == "archived"
    assert terminal_cut
    assert await _query("SELECT * FROM raw_captures ORDER BY uuid;") == terminal_cut
    own_capture = next(row for row in terminal_cut if row["uuid"] == parent.memory.id)
    graph_rows = await ordinary_graph.client.execute_query(
        "SELECT attributes FROM entity WHERE uuid=$id;",
        id=own_capture["metadata"].get("promoted_entity_id", "unreserved"),
    )
    if phase not in {"graph_crash", "final_crash"}:
        assert all(not graph_metadata_recallable(row["attributes"]) for row in graph_rows)
    if graph_rows:
        await _assert_retired_graph_read_denied(
            ordinary_graph, own_capture["metadata"]["promoted_entity_id"]
        )


async def _assert_retired_graph_read_denied(runtime, entity_id):
    from sibyl_core.services.graph_derivations import load_graph_projection_source
    from sibyl_core.services.source_observations import SourceUnavailableError

    async def read():
        return await load_graph_projection_source(
            runtime.client, organization_id="dream-org", source_id=entity_id
        )

    with pytest.raises(SourceUnavailableError):
        await read()
    stored = await runtime.entity_manager.get(entity_id)
    forged = {**stored.metadata["reflection_identity"], "review_capture_id": "unrelated"}
    await runtime.entity_manager.update(
        entity_id,
        {"metadata": {"review_capture_id": "unrelated", "reflection_identity": forged}},
    )
    with pytest.raises(SourceUnavailableError):
        await read()
    await runtime.entity_manager.update(
        entity_id, {"metadata": {"review_capture_id": None, "reflection_identity": None}}
    )
    with pytest.raises(SourceUnavailableError):
        await read()


@pytest.mark.parametrize("terminal", [False, True])
async def test_legacy_publication_obeys_own_terminal_record(
    corrected_chain, dream_store, ordinary_graph, monkeypatch, terminal
):
    from sibyl_core.migrate.scope_backfill import _recovered
    from sibyl_core.models.entities import Entity, EntityType
    from sibyl_core.services import memory_reflection
    from sibyl_core.services.automatic_reflection import _abstain
    from sibyl_core.services.graph_capture_availability import available_capture_projection_rows
    from sibyl_core.services.graph_derivations import (
        graph_publication_verdicts,
        reflection_candidate_current,
    )
    from sibyl_core.services.graph_read_validation import GraphReadValidation

    parent, resolver, _, _, _ = corrected_chain
    create = ordinary_graph.entity_manager.create_direct_if_absent
    created = None

    async def historical_writer(entity, **kwargs):
        nonlocal created
        if terminal:
            current = await prepare_stored_reflection(
                "dream-org", "owner", parent.memory.id, resolver
            )
            await _abstain(current, resolver, parent.memory.id)
        # Historical graph publications predate protected derivation records.
        created, _ = await create(entity)
        raise RuntimeError("historical publication boundary")

    monkeypatch.setattr(ordinary_graph.entity_manager, "create_direct_if_absent", historical_writer)
    with pytest.raises(RuntimeError, match="historical publication boundary"):
        await memory_reflection.promote_reflection_candidate_review(
            organization_id="dream-org",
            principal_id="owner",
            candidate_id=parent.memory.id,
            promote_to_scope="private",
        )
    assert created is not None
    assert not created.derivation_required

    def reader():
        return GraphReadValidation(
            "dream-org",
            content_execute_query=dream_store.execute_query,
            graph_execute_query=ordinary_graph.client.execute_query,
            source_authority_resolver=resolver,
        )

    verdicts = await graph_publication_verdicts(
        "dream-org", {created.id: created}, client=ordinary_graph.client, read=reader()
    )
    assert verdicts[created.id] is (False if terminal else None)
    available = await available_capture_projection_rows(
        "dream-org", {created.id: created}, graph_client=ordinary_graph.client, read=reader()
    )
    assert (created.id in available) is not terminal

    # Backfilled audience fields do not change the bound original candidate.
    backfilled = created.model_copy(
        update={"metadata": _recovered(created.metadata, "private", None, "new-owner")}
    )
    assert backfilled.metadata["principal_id"] == "new-owner"
    assert await reflection_candidate_current(backfilled, reader()) is not terminal

    legacy_child = Entity(
        id="legacy-descendant",
        entity_type=EntityType.PATTERN,
        name="Legacy descendant",
        content="Retained graph ancestry",
        organization_id="dream-org",
        metadata={"parent_entity_id": created.id},
    )
    legacy_child, _ = await create(legacy_child)
    available = await available_capture_projection_rows(
        "dream-org",
        {legacy_child.id: legacy_child},
        graph_client=ordinary_graph.client,
        read=reader(),
    )
    assert (legacy_child.id in available) is not terminal

    # Stripped mutable dependency hints cannot turn a native output into an
    # unrelated legacy row and skip its own-candidate terminal proof.
    await ordinary_graph.client.execute_query(
        "UPDATE entity SET attributes={} WHERE uuid=$id;", id=created.id
    )
    stripped = created.model_copy(update={"metadata": {}})
    assert not await available_capture_projection_rows(
        "dream-org", {created.id: stripped}, graph_client=ordinary_graph.client, read=reader()
    )


async def test_nonnative_reflection_like_identifier_stays_ordinary():
    from sibyl_core.models.entities import Entity, EntityType
    from sibyl_core.services.graph_capture_availability import available_capture_projection_rows
    from sibyl_core.services.graph_derivations import reflection_candidate_current

    entity = Entity(
        id="pattern_v2_legacy-title",
        entity_type=EntityType.PATTERN,
        name="Ordinary legacy pattern",
        content="No native reflection identity",
        organization_id="dream-org",
    )
    assert await reflection_candidate_current(entity, None)
    assert await available_capture_projection_rows("dream-org", {entity.id: entity}) == {
        entity.id: entity
    }
