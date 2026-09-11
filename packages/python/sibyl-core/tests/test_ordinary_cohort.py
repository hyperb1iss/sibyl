"""Durable ordinary proposals use real retained sources and existing stage owners."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.services import ordinary_cohort as service
from sibyl_core.services import procedure_validation
from sibyl_core.services.content_raw_persistence import remember_raw_memory, save_raw_memory
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.reflection_validation import (
    prepare_stored_reflection,
    validate_reflection_stage,
)
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.tasks.memory_validation import CriticOutput
from tests.test_eval_publication_promotion import runtime as runtime


@pytest.fixture
async def content_store(monkeypatch):
    client = SurrealContentClient(url="memory://")
    try:
        await bootstrap_content_schema(client, reset=True)

        @asynccontextmanager
        async def session():
            yield client

        monkeypatch.setattr("sibyl_core.services.content_client.surreal_content_client", session)
        yield client
    finally:
        await client.close()


@pytest.fixture
async def cohort_sources(content_store, monkeypatch):
    from sibyl_core.services import content_models

    monkeypatch.setattr(content_models, "configured_raw_memory_embedding_provider", lambda: None)
    return [
        await remember_raw_memory(
            organization_id="org",
            principal_id="owner",
            source_id=f"source-{i}",
            raw_content=f"Capture {i}: inspect logs before changing configuration.",
            embedding_provider=None,
        )
        for i in range(2)
    ]


def install_proposal(monkeypatch, sources):
    assertion = {
        "statement": "Inspect logs before changing configuration",
        "label": "inferred",
        "support": [
            {
                "episode_id": sources[0].id,
                "start_byte": 0,
                "end_byte": len(sources[0].raw_content.encode()),
            }
        ],
    }
    output = {"procedure": {"kind": "pattern", "goal": assertion}, "abstention_reason": None}
    owned = Extractor(
        CriticOutput, agent=Agent(TestModel(custom_output_args=output), output_type=CriticOutput)
    )
    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(return_value=(owned, '{"model":"offline"}')),
    )
    return output


async def test_ordinary_cohort_actual_stage_replay_and_critic(
    cohort_sources, monkeypatch, content_store, runtime
):
    sources = cohort_sources
    install_proposal(monkeypatch, sources)
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    authorize = AsyncMock()
    args = ("org", "owner", [s.id for s in sources], resolver)
    first, execution = await service.propose_stored_cohort(*args, authorize=authorize)
    second, replayed = await service.propose_stored_cohort(*args, authorize=authorize)
    assert first and second and first.id == second.id and execution == replayed
    stages = await content_store.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 1 and stages[0]["state"] == "returned"
    assert json.loads(stages[0]["result_json"])["usage"]["requests"] == 1
    assert json.loads(stages[0]["request_json"])["kind"] == "sibyl-ordinary-partial-proposal-v1"
    assert stages[0]["parent_id"] in [s.id for s in sources]
    parent = await prepare_stored_reflection("org", "owner", first.id, resolver)
    assert {s.id for s in parent.sources} == {s.id for s in sources}
    critic = Extractor(
        CriticOutput,
        agent=Agent(TestModel(custom_output_args={"findings": []}), output_type=CriticOutput),
    )
    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(return_value=(critic, '{"model":"offline"}')),
    )
    result = await validate_reflection_stage(parent, resolver)
    assert result["status"] == "no_findings"
    stage = await service.ValidationExecution(result["execution_id"], "org", "owner").load()
    request = json.loads(stage["request_json"])
    assert request["kind"] == "ordinary_reflection_validation-v2"
    assert request["input"] == result["input_sha256"]
    from sibyl_core.services.ordinary_publication import ordinary_promotion_binding

    promotion = await ordinary_promotion_binding(
        "org", "owner", first.id, result["execution_id"], resolver, authorize
    )
    guard, params = await promotion.current_guard()
    assert (
        "publication_source_observation_changed" in guard
        and params["ordinary_source_ids"] == parent.source_ids
    )
    from sibyl_core.services.memory_reflection import promote_reflection_candidate_review

    promoted = await promote_reflection_candidate_review(
        organization_id="org",
        principal_id="owner",
        candidate_id=first.id,
        promote_to_scope="private",
        promote_to_scope_key="owner",
        validation_promotion=promotion,
    )
    assert promoted.success
    from sibyl_core.services.validation_promotion import validated_graph_current

    assert await validated_graph_current("org", promoted.promoted_id)
    replay = await promote_reflection_candidate_review(
        organization_id="org",
        principal_id="owner",
        candidate_id=first.id,
        promote_to_scope="private",
        promote_to_scope_key="owner",
        validation_promotion=promotion,
    )
    assert replay.success and replay.promoted_id == promoted.promoted_id


async def test_ordinary_cohort_source_change_fences_returned_usage(
    cohort_sources, monkeypatch, content_store
):
    sources = cohort_sources
    install_proposal(monkeypatch, sources)
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    authorize = AsyncMock()
    extract = Extractor.extract_with_usage

    async def change(self, prompt):
        result = await extract(self, prompt)
        await save_raw_memory(
            replace(sources[0], raw_content="new source body"),
            expected_revision=sources[0].revision,
            embedding_provider=None,
        )
        return result

    monkeypatch.setattr(Extractor, "extract_with_usage", change)
    with pytest.raises(SourceUnavailableError):
        await service.propose_stored_cohort(
            "org", "owner", [s.id for s in sources], resolver, authorize=authorize
        )
    stages = await content_store.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 1 and stages[0]["state"] == "fenced"
    assert json.loads(stages[0]["usage_json"])["requests"] == 1
    assert not await content_store.execute_query(
        "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate';"
    )


async def test_ordinary_cohort_foreign_authority_no_stage(
    cohort_sources, monkeypatch, content_store
):
    install_proposal(monkeypatch, cohort_sources)
    with pytest.raises(SourceUnavailableError):
        await service.propose_stored_cohort(
            "org",
            "owner",
            [s.id for s in cohort_sources],
            AsyncMock(return_value=None),
            authorize=AsyncMock(),
        )
    assert not await content_store.execute_query("SELECT * FROM memory_validation_executions;")


async def test_ordinary_cohort_metadata_cannot_grant_confidence_policy():
    from types import SimpleNamespace

    from sibyl_core.services.content_models import MemoryScope
    from sibyl_core.services.memory_autonomy import decide_reflection_candidate_autonomy

    preview = SimpleNamespace(
        allowed=True,
        candidate_id="candidate",
        reason="allowed",
        review_state="pending",
        memory_scope=MemoryScope.PRIVATE,
        scope_key="owner",
        raw_source_ids=["source"],
        metadata={
            "confidence": 0,
            "autonomy_confidence_basis": "validated_source_support",
            "validated_source_support": True,
        },
    )
    assert not decide_reflection_candidate_autonomy(preview).should_promote
    assert decide_reflection_candidate_autonomy(
        preview, validated_source_support=True
    ).should_promote
    preview.metadata["sensitivity_flags"] = ["secret"]
    assert not decide_reflection_candidate_autonomy(
        preview, validated_source_support=True
    ).should_promote


@pytest.mark.parametrize("raced", [False, True])
async def test_ordinary_cohort_native_candidate_source_race(
    cohort_sources, content_store, monkeypatch, raced
):
    import asyncio
    import os

    if not os.environ.get("SIBYL_COHORT_NATIVE_URL"):
        pytest.skip("native multi-connection transaction control")
    install_proposal(monkeypatch, cohort_sources)
    execute = content_store.execute_query
    mutated = False

    async def delayed(query, **kwargs):
        nonlocal mutated
        if "Correction stage changed" not in query or mutated:
            return await execute(query, **kwargs)
        mutated = True
        pending = asyncio.create_task(
            execute(query.replace("LET $stage=", "SLEEP 1s; LET $stage=", 1), **kwargs)
        )
        try:
            if raced:
                await asyncio.sleep(0.25)
                changed = await execute(
                    "UPDATE raw_captures SET raw_content='concurrent committed source',revision+=1 WHERE uuid=$source RETURN AFTER;",
                    source=cohort_sources[0].id,
                )
                assert len(changed) == 1
            return await pending
        finally:
            if not pending.done():
                await pending

    monkeypatch.setattr(content_store, "execute_query", delayed)

    async def run():
        return await service.propose_stored_cohort(
            "org",
            "owner",
            [s.id for s in cohort_sources],
            AsyncMock(return_value=SourceReadAuthority("owner")),
            authorize=AsyncMock(),
        )

    if raced:
        with pytest.raises(Exception, match="cohort sources changed"):
            await run()
        assert not await execute(
            "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate';"
        )
        assert not await execute("SELECT * FROM memory_derivations;")
        rows = await execute(
            "SELECT raw_content FROM raw_captures WHERE uuid=$source;", source=cohort_sources[0].id
        )
        assert rows[0]["raw_content"] == "concurrent committed source"
    else:
        candidate, _ = await run()
        assert candidate is not None
    stages = await execute("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 1 and stages[0]["state"] == "returned"
    assert json.loads(stages[0]["usage_json"])["requests"] == 1


async def test_ordinary_cohort_completed_receipt_recovers_without_provider(
    cohort_sources, monkeypatch, content_store
):
    from sibyl_core.services.validation_execution import ValidationExecution

    install_proposal(monkeypatch, cohort_sources)
    original_finish = ValidationExecution._finish
    original_reconcile = ValidationExecution.reconcile_result
    monkeypatch.setattr(
        ValidationExecution, "_finish", AsyncMock(side_effect=OSError("database unavailable"))
    )
    monkeypatch.setattr(
        ValidationExecution,
        "reconcile_result",
        AsyncMock(side_effect=OSError("database unavailable")),
    )
    args = (
        "org",
        "owner",
        [s.id for s in cohort_sources],
        AsyncMock(return_value=SourceReadAuthority("owner")),
    )
    with pytest.raises(OSError, match="database unavailable"):
        await service.propose_stored_cohort(*args, authorize=AsyncMock())
    monkeypatch.setattr(ValidationExecution, "_finish", original_finish)
    monkeypatch.setattr(ValidationExecution, "reconcile_result", original_reconcile)
    monkeypatch.setattr(
        Extractor, "extract_with_usage", AsyncMock(side_effect=AssertionError("redispatch"))
    )
    candidate, _ = await service.propose_stored_cohort(*args, authorize=AsyncMock())
    assert candidate is not None
    stages = await content_store.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 1 and stages[0]["state"] == "returned"
    assert json.loads(stages[0]["usage_json"])["requests"] == 1


async def test_ordinary_cohort_budget_partition_keeps_all_source_bytes(
    cohort_sources, content_store, monkeypatch
):
    from sibyl_core.config import settings
    from sibyl_core.tasks._evidence_json import canonical
    from sibyl_core.tasks.ordinary_proposals import PartialProposal

    sources = [
        await remember_raw_memory(
            organization_id="org",
            principal_id="owner",
            source_id=f"large-{i}",
            raw_content=f"capture-{i}:" + "a" * 3000,
            embedding_provider=None,
        )
        for i in range(20)
    ]
    install_proposal(monkeypatch, sources)
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    monkeypatch.setattr(settings, "consolidation_max_input_chars", 40000)
    ids = [s.id for s in sources]
    bins = await service.partition_stored_cohort("org", "owner", ids, resolver)
    assert len(bins) > 1
    assert sorted(i for bucket in bins for i in bucket) == sorted(ids)
    assert bins == await service.partition_stored_cohort(
        "org", "owner", list(reversed(ids)), resolver
    )
    for bucket in bins:
        if len(bucket) < 2:
            continue
        prepared = await service.prepare_stored_cohort("org", "owner", bucket, resolver)
        from sibyl_core.ai.llm.extractor import extraction_schema

        assert (
            len(prepared.prepared.system)
            + len(prepared.prepared.prompt)
            + len(canonical(extraction_schema(PartialProposal)))
            <= 40000
        )
        for source in sources:
            if source.id in bucket:
                assert source.raw_content in prepared.prepared.prompt
