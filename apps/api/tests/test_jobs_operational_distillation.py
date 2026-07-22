from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl.jobs import operational_distillation
from sibyl_core.ai.llm import ExtractionUsage
from sibyl_core.ai.operational_distillation import (
    DistilledOperationalNotes,
    operational_distilled_note_id,
)
from sibyl_core.models.experience import (
    OperationalEvidencePart,
    OperationalExperience,
    OperationalObservation,
)
from sibyl_core.projection import project_operational_experience


def _experience() -> OperationalExperience:
    return OperationalExperience(
        source_id="capture-1",
        goal="Close incident INC001",
        outcome="success",
        project_id="project-1",
        observations=(
            OperationalObservation(
                id="state-0",
                ordinal=0,
                action="set Status to Closed",
                evidence=(OperationalEvidencePart(id="tree-0", content="Status: Closed"),),
            ),
        ),
    )


class _Extractor:
    async def extract_with_usage(self, prompt: str) -> SimpleNamespace:
        assert "Close incident INC001" in prompt
        return SimpleNamespace(
            output=DistilledOperationalNotes(
                workflow="Open INC001 and set Status to Closed.",
                facts=["INC001 has a Status field."],
            ),
            usage=ExtractionUsage(
                provider="openai",
                model="gpt-5.4-nano",
                requests=1,
                input_tokens=120,
                output_tokens=40,
                total_tokens=160,
            ),
        )


@asynccontextmanager
async def _acquired_lock(*_args: object, **_kwargs: object):
    yield "lock-token"


@pytest.mark.asyncio
async def test_distillation_writes_current_notes_and_removes_stale_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experience = _experience()
    projection = project_operational_experience(experience)
    manifest = next(
        entity
        for entity in projection.entities
        if entity.id == projection.manifest.manifest_entity_id
    )
    entity_manager = SimpleNamespace(
        get=AsyncMock(return_value=manifest),
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        ),
        delete=AsyncMock(side_effect=lambda entity_id: entity_id.endswith("irrelevant")),
    )
    runtime = SimpleNamespace(entity_manager=entity_manager)
    monkeypatch.setattr(operational_distillation, "entity_lock", _acquired_lock)
    monkeypatch.setattr(
        operational_distillation,
        "get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    monkeypatch.setattr(
        operational_distillation,
        "operational_note_distiller",
        lambda **_: _Extractor(),
    )

    result = await operational_distillation.distill_operational_experience_notes(
        {},
        experience.model_dump(mode="json"),
        "org-1",
        content_hash=manifest.metadata["operational_content_hash"],
        created_by="user-1",
        max_tokens=512,
    )

    assert result["status"] == "complete"
    assert result["provider"] == "openai"
    assert result["model"] == "gpt-5.4-nano"
    written = entity_manager.create_direct_bulk.await_args.args[0]
    assert [entity.metadata["note_kind"] for entity in written] == ["workflow", "facts"]
    assert all(entity.metadata["project_id"] == "project-1" for entity in written)
    assert entity_manager.create_direct_bulk.await_args.kwargs == {"generate_embeddings": True}
    entity_manager.delete.assert_awaited_once_with(
        operational_distilled_note_id("capture-1", "gotchas")
    )


@pytest.mark.asyncio
async def test_distillation_drops_output_when_manifest_changes_during_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experience = _experience()
    projection = project_operational_experience(experience)
    manifest = next(
        entity
        for entity in projection.entities
        if entity.id == projection.manifest.manifest_entity_id
    )
    stale_manifest = manifest.model_copy(
        update={
            "metadata": {
                **manifest.metadata,
                "operational_content_hash": "newer-content-hash",
            }
        }
    )
    entity_manager = SimpleNamespace(
        get=AsyncMock(side_effect=[manifest, stale_manifest]),
        create_direct_bulk=AsyncMock(),
        delete=AsyncMock(),
    )
    monkeypatch.setattr(operational_distillation, "entity_lock", _acquired_lock)
    monkeypatch.setattr(
        operational_distillation,
        "get_surreal_graph_runtime",
        AsyncMock(return_value=SimpleNamespace(entity_manager=entity_manager)),
    )
    monkeypatch.setattr(
        operational_distillation,
        "operational_note_distiller",
        lambda **_: _Extractor(),
    )

    result = await operational_distillation.distill_operational_experience_notes(
        {},
        experience.model_dump(mode="json"),
        "org-1",
        content_hash=manifest.metadata["operational_content_hash"],
        created_by="user-1",
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "manifest_stale"
    entity_manager.create_direct_bulk.assert_not_awaited()
    entity_manager.delete.assert_not_awaited()
