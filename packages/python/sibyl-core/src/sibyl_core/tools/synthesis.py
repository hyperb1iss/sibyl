"""MCP-friendly source-grounded synthesis tool wrappers."""

from __future__ import annotations

from typing import Any

from sibyl_core.models.synthesis import (
    SynthesisArtifactFormat,
    SynthesisDepth,
    SynthesisOutputType,
    SynthesisRequest,
    SynthesisSectionRequest,
)
from sibyl_core.services import synthesis as synthesis_service


def _coerce_required_sections(
    required_sections: list[dict[str, Any] | str] | None,
) -> list[SynthesisSectionRequest]:
    sections: list[SynthesisSectionRequest] = []
    for item in required_sections or []:
        if isinstance(item, str):
            title, _, prompt = item.partition("::")
            sections.append(
                SynthesisSectionRequest(
                    title=title.strip(),
                    prompt=prompt.strip() or None,
                )
            )
            continue
        sections.append(
            SynthesisSectionRequest(
                title=str(item.get("title") or "").strip(),
                prompt=str(item["prompt"]).strip() if item.get("prompt") else None,
                required_source_ids=[
                    str(source_id)
                    for source_id in item.get("required_source_ids", [])
                    if str(source_id).strip()
                ],
            )
        )
    return [section for section in sections if section.title]


def _synthesis_request(
    *,
    goal: str,
    output_type: str = SynthesisOutputType.DOCUMENTATION.value,
    audience: str | None = None,
    depth: str = SynthesisDepth.STANDARD.value,
    seed_query: str | None = None,
    project: str | None = None,
    domain: str | None = None,
    entity_ids: list[str] | None = None,
    decision_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    artifact_ids: list[str] | None = None,
    required_sections: list[dict[str, Any] | str] | None = None,
    constraints: list[str] | None = None,
    max_sections: int = 6,
    include_neighborhoods: bool = True,
) -> SynthesisRequest:
    return SynthesisRequest(
        goal=goal,
        output_type=SynthesisOutputType(output_type),
        audience=audience,
        depth=SynthesisDepth(depth),
        seed_query=seed_query,
        project=project,
        domain=domain,
        entity_ids=list(entity_ids or []),
        decision_ids=list(decision_ids or []),
        task_ids=list(task_ids or []),
        artifact_ids=list(artifact_ids or []),
        required_sections=_coerce_required_sections(required_sections),
        constraints=list(constraints or []),
        max_sections=max_sections,
        include_neighborhoods=include_neighborhoods,
    )


async def _planned_materialized_run(
    request: SynthesisRequest,
    *,
    organization_id: str,
    principal_id: str | None,
    accessible_projects: set[str] | None = None,
) -> Any:
    run = await synthesis_service.plan_synthesis(
        request,
        organization_id=organization_id,
        accessible_projects=accessible_projects,
        search_fn=synthesis_service.default_search,
        related_fn=synthesis_service.default_related_sources,
    )
    return await synthesis_service.materialize_synthesis_section_packs(
        run,
        organization_id=organization_id,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        context_fn=synthesis_service.default_context_pack,
    )


async def synthesis_plan(
    *,
    goal: str,
    organization_id: str,
    principal_id: str | None = None,
    accessible_projects: set[str] | None = None,
    output_type: str = SynthesisOutputType.DOCUMENTATION.value,
    audience: str | None = None,
    depth: str = SynthesisDepth.STANDARD.value,
    seed_query: str | None = None,
    project: str | None = None,
    domain: str | None = None,
    entity_ids: list[str] | None = None,
    decision_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    artifact_ids: list[str] | None = None,
    required_sections: list[dict[str, Any] | str] | None = None,
    constraints: list[str] | None = None,
    max_sections: int = 6,
    include_neighborhoods: bool = True,
) -> dict[str, Any]:
    """Plan and materialize source packs for an authorized synthesis request."""

    request = _synthesis_request(
        goal=goal,
        output_type=output_type,
        audience=audience,
        depth=depth,
        seed_query=seed_query,
        project=project,
        domain=domain,
        entity_ids=entity_ids,
        decision_ids=decision_ids,
        task_ids=task_ids,
        artifact_ids=artifact_ids,
        required_sections=required_sections,
        constraints=constraints,
        max_sections=max_sections,
        include_neighborhoods=include_neighborhoods,
    )
    run = await _planned_materialized_run(
        request,
        organization_id=organization_id,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
    )
    return synthesis_service.synthesis_run_to_dict(run)


async def synthesis_verify(
    *,
    goal: str,
    organization_id: str,
    principal_id: str | None = None,
    accessible_projects: set[str] | None = None,
    output_type: str = SynthesisOutputType.DOCUMENTATION.value,
    audience: str | None = None,
    depth: str = SynthesisDepth.STANDARD.value,
    seed_query: str | None = None,
    project: str | None = None,
    domain: str | None = None,
    entity_ids: list[str] | None = None,
    decision_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    artifact_ids: list[str] | None = None,
    required_sections: list[dict[str, Any] | str] | None = None,
    constraints: list[str] | None = None,
    max_sections: int = 6,
    include_neighborhoods: bool = True,
) -> dict[str, Any]:
    """Verify a materialized synthesis request without persisting an artifact."""

    request = _synthesis_request(
        goal=goal,
        output_type=output_type,
        audience=audience,
        depth=depth,
        seed_query=seed_query,
        project=project,
        domain=domain,
        entity_ids=entity_ids,
        decision_ids=decision_ids,
        task_ids=task_ids,
        artifact_ids=artifact_ids,
        required_sections=required_sections,
        constraints=constraints,
        max_sections=max_sections,
        include_neighborhoods=include_neighborhoods,
    )
    run = await _planned_materialized_run(
        request,
        organization_id=organization_id,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
    )
    run = synthesis_service.apply_synthesis_verification(run)
    return synthesis_service.synthesis_run_to_dict(run)


async def synthesis_draft(
    *,
    goal: str,
    organization_id: str,
    principal_id: str | None = None,
    accessible_projects: set[str] | None = None,
    output_type: str = SynthesisOutputType.DOCUMENTATION.value,
    audience: str | None = None,
    depth: str = SynthesisDepth.STANDARD.value,
    seed_query: str | None = None,
    project: str | None = None,
    domain: str | None = None,
    entity_ids: list[str] | None = None,
    decision_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    artifact_ids: list[str] | None = None,
    required_sections: list[dict[str, Any] | str] | None = None,
    constraints: list[str] | None = None,
    max_sections: int = 6,
    include_neighborhoods: bool = True,
    output_format: str = SynthesisArtifactFormat.MARKDOWN.value,
    remember: bool = False,
    memory_scope: str = "private",
    scope_key: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Draft, verify, and optionally remember a source-grounded artifact."""

    request = _synthesis_request(
        goal=goal,
        output_type=output_type,
        audience=audience,
        depth=depth,
        seed_query=seed_query,
        project=project,
        domain=domain,
        entity_ids=entity_ids,
        decision_ids=decision_ids,
        task_ids=task_ids,
        artifact_ids=artifact_ids,
        required_sections=required_sections,
        constraints=constraints,
        max_sections=max_sections,
        include_neighborhoods=include_neighborhoods,
    )
    run = await _planned_materialized_run(
        request,
        organization_id=organization_id,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
    )
    artifact = synthesis_service.draft_synthesis_artifact(
        run,
        output_format=SynthesisArtifactFormat(output_format),
    )
    run = synthesis_service.apply_synthesis_verification(run)
    if remember:
        if not principal_id:
            raise ValueError("principal_id is required")
        artifact = await synthesis_service.remember_synthesis_artifact(
            artifact,
            run,
            organization_id=organization_id,
            principal_id=principal_id,
            memory_scope=memory_scope,
            scope_key=scope_key,
            tags=tags,
            remember_fn=synthesis_service.default_remember_artifact,
        )
    payload = synthesis_service.synthesis_run_to_dict(run)
    payload["artifact"] = synthesis_service.synthesis_artifact_to_dict(artifact)
    return payload


__all__ = [
    "synthesis_draft",
    "synthesis_plan",
    "synthesis_verify",
]
