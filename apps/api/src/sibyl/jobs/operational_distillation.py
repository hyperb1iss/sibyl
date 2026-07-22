"""Queued distillation of operational experiences into reusable notes."""

from __future__ import annotations

import time
from typing import Any

import structlog

from sibyl.locks import entity_lock
from sibyl_core.ai.llm.budget import llm_budget_context
from sibyl_core.ai.operational_distillation import (
    build_operational_experience_digest,
    build_operational_note_distillation_prompt,
    build_operational_note_entities,
    operational_distilled_note_id,
    operational_note_distiller,
)
from sibyl_core.models.experience import OperationalExperience
from sibyl_core.observability import elapsed_ms
from sibyl_core.projection import (
    MANIFEST_STATE_COMPLETE,
    MANIFEST_STATE_EMBEDDING_PENDING,
    operational_experience_manifest_id,
)
from sibyl_core.services.graph import get_surreal_graph_runtime

log = structlog.get_logger()
_NOTE_KINDS = frozenset({"workflow", "facts", "gotchas"})


async def distill_operational_experience_notes(
    ctx: dict[str, Any],  # noqa: ARG001
    experience_data: dict[str, Any],
    group_id: str,
    *,
    content_hash: str,
    created_by: str | None,
    max_tokens: int = 2_048,
) -> dict[str, Any]:
    """Distill one current operational capture into deterministic note entities."""
    started_at = time.perf_counter()
    experience = OperationalExperience.model_validate(experience_data)
    runtime = await get_surreal_graph_runtime(group_id)
    manifest_id = operational_experience_manifest_id(experience.source_id)
    state = await _manifest_state(
        runtime.entity_manager,
        manifest_id=manifest_id,
        source_id=experience.source_id,
        project_id=experience.project_id,
        content_hash=content_hash,
    )
    if state != "current":
        return _skipped_result(
            experience=experience,
            group_id=group_id,
            state=state,
            started_at=started_at,
        )

    prompt = build_operational_note_distillation_prompt(
        build_operational_experience_digest(experience)
    )
    extractor = operational_note_distiller(max_tokens=max_tokens)
    with llm_budget_context(user_id=created_by, organization_id=group_id):
        extraction = await extractor.extract_with_usage(prompt)
    notes = extraction.output
    entities = build_operational_note_entities(
        notes,
        experience=experience,
        organization_id=group_id,
        created_by=created_by,
        content_hash=content_hash,
        provider=extraction.usage.provider,
        model=extraction.usage.model,
    )

    async with entity_lock(group_id, manifest_id, blocking=True):
        state = await _manifest_state(
            runtime.entity_manager,
            manifest_id=manifest_id,
            source_id=experience.source_id,
            project_id=experience.project_id,
            content_hash=content_hash,
        )
        if state != "current":
            return _skipped_result(
                experience=experience,
                group_id=group_id,
                state=state,
                started_at=started_at,
            )
        written_ids = await runtime.entity_manager.create_direct_bulk(
            entities,
            generate_embeddings=True,
        )
        expected_ids = {entity.id for entity in entities}
        if set(written_ids) != expected_ids:
            raise RuntimeError("failed to persist every distilled operational note")
        emitted_kinds = {str(entity.metadata["note_kind"]) for entity in entities}
        stale_ids = [
            operational_distilled_note_id(experience.source_id, note_kind)
            for note_kind in sorted(_NOTE_KINDS - emitted_kinds)
        ]
        deleted_ids = [
            note_id for note_id in stale_ids if await runtime.entity_manager.delete(note_id)
        ]

    result = {
        "group_id": group_id,
        "source_id": experience.source_id,
        "content_hash": content_hash,
        "status": "complete",
        "written_note_ids": sorted(written_ids),
        "deleted_note_ids": deleted_ids,
        "provider": extraction.usage.provider,
        "model": extraction.usage.model,
        "input_tokens": extraction.usage.input_tokens,
        "output_tokens": extraction.usage.output_tokens,
        "duration_ms": elapsed_ms(started_at),
    }
    log.info("operational_note_distillation_complete", **result)
    return result


async def _manifest_state(
    entity_manager: Any,
    *,
    manifest_id: str,
    source_id: str,
    project_id: str | None,
    content_hash: str,
) -> str:
    try:
        manifest = await entity_manager.get(manifest_id)
    except KeyError:
        return "missing"
    metadata = manifest.metadata
    if (
        metadata.get("operational_source_id") != source_id
        or metadata.get("operational_content_hash") != content_hash
        or metadata.get("project_id") != project_id
    ):
        return "stale"
    manifest_state = metadata.get("operational_projection_state")
    if manifest_state not in {
        MANIFEST_STATE_COMPLETE,
        MANIFEST_STATE_EMBEDDING_PENDING,
    }:
        return "uncommitted"
    return "current"


def _skipped_result(
    *,
    experience: OperationalExperience,
    group_id: str,
    state: str,
    started_at: float,
) -> dict[str, Any]:
    result = {
        "group_id": group_id,
        "source_id": experience.source_id,
        "status": "skipped",
        "reason": f"manifest_{state}",
        "written_note_ids": [],
        "deleted_note_ids": [],
        "duration_ms": elapsed_ms(started_at),
    }
    log.info("operational_note_distillation_skipped", **result)
    return result


__all__ = ["distill_operational_experience_notes"]
