"""The ordinary dream job resumes staged work and selects source corrections."""

from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from sibyl.jobs import reflection
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.services.surreal_content import remember_raw_memory, save_raw_memory


@pytest.fixture
async def dream_store(monkeypatch):
    client = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(client, reset=True)

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr("sibyl_core.services.content_client.surreal_content_client", session)
    monkeypatch.setattr(
        "sibyl_core.services.content_models.configured_raw_memory_embedding_provider", lambda: None
    )
    monkeypatch.setattr(reflection, "_resolve_accessible_projects", AsyncMock(return_value=set()))
    try:
        yield client
    finally:
        await client.close()


async def test_dream_job_resume_keeps_candidate_identity(dream_store, monkeypatch):
    source = await remember_raw_memory(
        organization_id="dream-org",
        principal_id="owner",
        source_id="session",
        raw_content="Decision: validate inputs before parsing.",
        embedding_provider=None,
    )
    real_complete = reflection.complete_dream_stage
    monkeypatch.setattr(reflection, "complete_dream_stage", AsyncMock(return_value=False))
    first = await reflection.run_reflection_dream_cycle({}, "dream-org", candidate_limit=0)
    assert first["failed"] == 1
    before = await dream_store.execute_query(
        "SELECT uuid, revision FROM raw_captures WHERE capture_surface='reflection_candidate';"
    )
    assert before
    monkeypatch.setattr(reflection, "complete_dream_stage", real_complete)
    second = await reflection.run_reflection_dream_cycle({}, "dream-org", candidate_limit=0)
    assert second["failed"] == 0
    assert second["sources_reflected"] == 1
    after = await dream_store.execute_query(
        "SELECT uuid, revision FROM raw_captures WHERE capture_surface='reflection_candidate';"
    )
    assert after == before
    third = await reflection.run_reflection_dream_cycle({}, "dream-org", candidate_limit=0)
    assert third["sources_scanned"] == 0
    await save_raw_memory(
        replace(source, raw_content="Decision: validate inputs and preserve original bytes."),
        expected_revision=source.revision,
        embedding_provider=None,
    )
    fourth = await reflection.run_reflection_dream_cycle({}, "dream-org", candidate_limit=0)
    assert fourth["sources_reflected"] == 1
    assert fourth["sources"][0]["operation_id"] != second["sources"][0]["operation_id"]


@pytest.mark.parametrize("change", ["source", "grants"])
async def test_dream_job_does_not_consume_concurrent_source_or_authority_change(
    dream_store, monkeypatch, change
):
    source = await remember_raw_memory(
        organization_id="dream-org",
        principal_id="owner",
        source_id="session",
        raw_content="Decision: validate inputs before parsing.",
        embedding_provider=None,
    )
    original = reflection.reflect_memory

    async def changing(*args, **kwargs):
        pack = await original(*args, **kwargs)
        if change == "source":
            await save_raw_memory(
                replace(source, raw_content="Corrected advice"),
                expected_revision=source.revision,
                embedding_provider=None,
            )
        else:
            monkeypatch.setattr(
                reflection, "_resolve_accessible_projects", AsyncMock(return_value={"new-project"})
            )
        return pack

    monkeypatch.setattr(reflection, "reflect_memory", changing)
    first = await reflection.run_reflection_dream_cycle({}, "dream-org", candidate_limit=0)
    assert first["failed"] == 1
    stages = await dream_store.execute_query(
        "SELECT completion_json FROM dream_source_checkpoints;"
    )
    assert len(stages) == 1
    assert not stages[0].get("completion_json")
    monkeypatch.setattr(reflection, "reflect_memory", original)
    second = await reflection.run_reflection_dream_cycle({}, "dream-org", candidate_limit=0)
    assert second["sources_reflected"] == 1
    stages = await dream_store.execute_query(
        "SELECT completion_json FROM dream_source_checkpoints;"
    )
    assert len(stages) == 2
    assert sum(bool(stage.get("completion_json")) for stage in stages) == 1


@pytest.mark.parametrize("phase", ["dispatch", "selection"])
async def test_dream_failed_source_remains_retryable_without_starving_others(
    dream_store, monkeypatch, phase
):
    sources = [
        await remember_raw_memory(
            organization_id="dream-org",
            principal_id="owner",
            source_id=str(index),
            raw_content="Decision: validate input.",
            embedding_provider=None,
        )
        for index in range(2)
    ]
    failed_id = min(memory.id for memory in sources)
    target = "_reflect_dream_source" if phase == "dispatch" else "load_dream_stage"
    original = getattr(reflection, target)
    failures = []

    async def failing(*args, **kwargs):
        source_id = kwargs["source"].id if phase == "dispatch" else args[0].snapshot.memory.id
        if source_id == failed_id:
            failures.append(source_id)
            raise ValueError("source-specific operational failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(reflection, target, failing)
    runs = [
        await reflection.run_reflection_dream_cycle(
            {}, "dream-org", source_limit=1, candidate_limit=0
        )
        for _ in range(3)
    ]
    assert sum(run["sources_reflected"] for run in runs) == 1
    assert sum(run["failed"] for run in runs) == 2
    assert len(failures) == 2
    assert {item["source_id"] for run in runs for item in run["sources"]} == {
        memory.id for memory in sources
    }
