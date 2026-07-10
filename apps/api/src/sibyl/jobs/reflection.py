"""Reflection maintenance jobs."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

import structlog

from sibyl.persistence.auth_runtime import (
    log_memory_audit_event,
    resolve_accessible_project_graph_ids,
)
from sibyl_core.models.reflection import ReflectionPack
from sibyl_core.services.memory import (
    ReflectionPromotionResult,
    preview_reflection_candidate_promotion,
    promote_reflection_candidate_review,
)
from sibyl_core.services.memory_autonomy import (
    ReflectionAutonomyOutcome,
    ReflectionAutonomyPolicy,
    decide_reflection_candidate_autonomy,
)
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    list_reflection_candidate_reviews,
    list_reflection_dream_source_memories,
    save_raw_memory,
)
from sibyl_core.tools.reflect import reflect_memory

log = structlog.get_logger()

_ARCHIVEABLE_EXCEPTION_REASONS = frozenset({"duplicate_candidate", "stale_candidate"})


async def run_reflection_dream_cycle_all_orgs(
    ctx: dict[str, Any],
    *,
    dry_run: bool = False,
    source_limit: int = 20,
    candidate_limit: int = 50,
    archive_exceptions: bool = True,
    confidence_threshold: float | None = None,
) -> dict[str, Any]:
    org_ids = await _list_organization_ids()
    results: list[dict[str, Any]] = []
    for org_id in org_ids:
        try:
            results.append(
                await run_reflection_dream_cycle(
                    ctx,
                    str(org_id),
                    dry_run=dry_run,
                    source_limit=source_limit,
                    candidate_limit=candidate_limit,
                    archive_exceptions=archive_exceptions,
                    confidence_threshold=confidence_threshold,
                )
            )
        except Exception as exc:
            log.warning(
                "reflection_dream_cycle_org_failed",
                group_id=str(org_id),
                error=str(exc),
                exc_info=True,
            )
            results.append(
                {
                    "group_id": str(org_id),
                    "outcome": "error",
                    "reason": str(exc),
                }
            )
    return {
        "orgs_processed": len(results),
        "orgs_succeeded": sum(1 for result in results if result.get("outcome") != "error"),
        "orgs_failed": sum(1 for result in results if result.get("outcome") == "error"),
        "dry_run": dry_run,
        "results": results,
    }


async def run_reflection_dream_cycle(
    ctx: dict[str, Any],  # noqa: ARG001
    group_id: str,
    *,
    dry_run: bool = False,
    source_limit: int = 20,
    candidate_limit: int = 50,
    archive_exceptions: bool = True,
    archive_exception_reasons: list[str] | None = None,
    confidence_threshold: float | None = None,
) -> dict[str, Any]:
    started = datetime.now(UTC)
    start_time = perf_counter()
    run_id = f"reflection_dream:{group_id}:{uuid4()}"
    source_budget = max(0, min(source_limit, 100))
    candidate_budget = max(0, min(candidate_limit, 200))
    archive_reasons = {
        reason
        for reason in (archive_exception_reasons or sorted(_ARCHIVEABLE_EXCEPTION_REASONS))
        if reason in _ARCHIVEABLE_EXCEPTION_REASONS
    }

    log.info(
        "reflection_dream_cycle_started",
        group_id=group_id,
        dry_run=dry_run,
        source_limit=source_budget,
        candidate_limit=candidate_budget,
        run_id=run_id,
    )

    source_results = await _reflect_dream_sources(
        group_id=group_id,
        run_id=run_id,
        dry_run=dry_run,
        limit=source_budget,
    )
    candidate_results = await _drain_dream_candidates(
        group_id=group_id,
        run_id=run_id,
        dry_run=dry_run,
        limit=candidate_budget,
        archive_exceptions=archive_exceptions,
        archive_reasons=archive_reasons,
        confidence_threshold=confidence_threshold,
    )

    finished = datetime.now(UTC)
    all_results = [*source_results, *candidate_results]
    receipt = {
        "run_id": run_id,
        "group_id": group_id,
        "dry_run": dry_run,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "latency_ms": round((perf_counter() - start_time) * 1000, 2),
        "source_limit": source_budget,
        "candidate_limit": candidate_budget,
        "sources_scanned": len(source_results),
        "sources_reflected": sum(1 for item in source_results if item["outcome"] == "reflected"),
        "candidates_scanned": len(candidate_results),
        "promoted": sum(1 for item in candidate_results if item["outcome"] == "auto_promote"),
        "archived": sum(1 for item in candidate_results if item.get("archived") is True),
        "exceptioned": sum(1 for item in candidate_results if item["outcome"] == "exception"),
        "skipped": sum(1 for item in all_results if item["outcome"] == "skip"),
        "failed": sum(1 for item in all_results if item["outcome"] == "error"),
        "model_usage": {
            "extractor": "sibyl_reflection_extractor",
            "metered": False,
        },
        "sources": source_results,
        "candidates": candidate_results,
    }
    log.info("reflection_dream_cycle_completed", **_summary_log_fields(receipt))
    return receipt


async def _reflect_dream_sources(
    *,
    group_id: str,
    run_id: str,
    dry_run: bool,
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    sources = await list_reflection_dream_source_memories(
        organization_id=group_id,
        limit=limit,
    )
    results: list[dict[str, Any]] = []
    for source in sources:
        try:
            results.append(
                await _reflect_dream_source(
                    source=source,
                    group_id=group_id,
                    run_id=run_id,
                    dry_run=dry_run,
                )
            )
        except Exception as exc:
            log.warning(
                "reflection_dream_source_failed",
                source_id=source.id,
                error=str(exc),
                exc_info=True,
            )
            results.append(
                {
                    "source_id": source.id,
                    "outcome": "error",
                    "reason": str(exc),
                }
            )
    return results


async def _reflect_dream_source(
    *,
    source: RawMemory,
    group_id: str,
    run_id: str,
    dry_run: bool,
) -> dict[str, Any]:
    if not source.principal_id:
        return await _mark_source_processed(
            source,
            run_id=run_id,
            dry_run=dry_run,
            outcome="skip",
            reason="missing_principal",
        )
    if not source.raw_content.strip():
        return await _mark_source_processed(
            source,
            run_id=run_id,
            dry_run=dry_run,
            outcome="skip",
            reason="empty_source",
        )

    accessible_projects = await _accessible_projects_for_source(group_id=group_id, source=source)
    pack = await reflect_memory(
        source.raw_content,
        source_title=source.title or source.source_id or source.id,
        intent="maintenance",
        domain=_metadata_str(source.metadata, "domain"),
        project=source.project_id,
        related_to=_metadata_str_list(source.metadata.get("related_to")),
        organization_id=group_id,
        principal_id=source.principal_id,
        accessible_projects=accessible_projects,
        memory_scope=source.memory_scope,
        scope_key=source.scope_key,
        persist=not dry_run,
        persist_source=False,
        persist_review=not dry_run,
        existing_source_id=source.id,
    )
    return await _mark_source_reflected(source, pack=pack, run_id=run_id, dry_run=dry_run)


async def _drain_dream_candidates(
    *,
    group_id: str,
    run_id: str,
    dry_run: bool,
    limit: int,
    archive_exceptions: bool,
    archive_reasons: set[str],
    confidence_threshold: float | None,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    candidates = await list_reflection_candidate_reviews(
        organization_id=group_id,
        review_state="pending",
        limit=limit,
    )
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            results.append(
                await _drain_dream_candidate(
                    candidate=candidate,
                    group_id=group_id,
                    run_id=run_id,
                    dry_run=dry_run,
                    archive_exceptions=archive_exceptions,
                    archive_reasons=archive_reasons,
                    confidence_threshold=confidence_threshold,
                )
            )
        except Exception as exc:
            log.warning(
                "reflection_dream_candidate_failed",
                candidate_id=candidate.id,
                error=str(exc),
                exc_info=True,
            )
            results.append(
                {
                    "candidate_id": candidate.id,
                    "outcome": "error",
                    "reason": str(exc),
                    "dry_run": dry_run,
                }
            )
    return results


async def _drain_dream_candidate(
    *,
    candidate: RawMemory,
    group_id: str,
    run_id: str,
    dry_run: bool,
    archive_exceptions: bool,
    archive_reasons: set[str],
    confidence_threshold: float | None,
) -> dict[str, Any]:
    target_scope = _candidate_target_scope(candidate)
    target_scope_key = _candidate_target_scope_key(candidate, target_scope)
    project = _candidate_project(
        candidate, target_scope=target_scope, target_scope_key=target_scope_key
    )
    accessible_projects = await _accessible_projects_for_candidate(
        group_id=group_id,
        candidate=candidate,
    )
    preview = await preview_reflection_candidate_promotion(
        candidate_id=candidate.id,
        organization_id=group_id,
        principal_id=candidate.principal_id,
        promote_to_scope=target_scope,
        promote_to_scope_key=target_scope_key,
        domain=_metadata_str(candidate.metadata, "domain"),
        project=project,
        accessible_projects=accessible_projects,
    )
    policy = ReflectionAutonomyPolicy(
        confidence_threshold=confidence_threshold
        if confidence_threshold is not None
        else ReflectionAutonomyPolicy().confidence_threshold
    )
    decision = decide_reflection_candidate_autonomy(
        preview,
        policy=policy,
        dry_run=dry_run,
    )
    promotion: ReflectionPromotionResult | None = None
    if decision.should_promote:
        promotion = await promote_reflection_candidate_review(
            candidate_id=candidate.id,
            organization_id=group_id,
            principal_id=candidate.principal_id,
            promote_to_scope=target_scope,
            promote_to_scope_key=target_scope_key,
            domain=_metadata_str(candidate.metadata, "domain"),
            project=project,
            related_to=_metadata_str_list(candidate.metadata.get("related_to")),
            accessible_projects=accessible_projects,
        )

    archived = False
    review_state = promotion.review_state if promotion else decision.review_state
    if (
        decision.outcome is ReflectionAutonomyOutcome.EXCEPTION
        and archive_exceptions
        and not dry_run
        and _archiveable_exception(decision.exception_reasons, archive_reasons=archive_reasons)
    ):
        archived_memory = await _archive_dream_exception_candidate(
            candidate=candidate,
            decision_reason=decision.reason,
            exception_reasons=decision.exception_reasons,
            run_id=run_id,
        )
        archived = True
        review_state = archived_memory.review_state

    await _log_dream_candidate_audit(
        candidate=candidate,
        group_id=group_id,
        run_id=run_id,
        dry_run=dry_run,
        preview_allowed=preview.allowed,
        decision_reason=decision.reason,
        outcome=decision.outcome.value,
        recommended_action=decision.recommended_action.value,
        memory_scope=decision.memory_scope.value if decision.memory_scope else target_scope,
        scope_key=decision.scope_key or target_scope_key,
        project=project,
        raw_source_ids=decision.raw_source_ids,
        promoted_id=promotion.promoted_id if promotion else None,
        exception_reasons=decision.exception_reasons,
        review_state=review_state,
    )
    return {
        "candidate_id": candidate.id,
        "outcome": decision.outcome.value,
        "recommended_action": decision.recommended_action.value,
        "applied": promotion is not None and promotion.success,
        "archived": archived,
        "dry_run": dry_run,
        "reason": decision.reason,
        "review_state": review_state,
        "promoted_id": promotion.promoted_id if promotion else None,
        "raw_source_ids": list(decision.raw_source_ids),
        "policy_reasons": list(decision.policy_reasons),
        "exception_reasons": list(decision.exception_reasons),
        "confidence": decision.confidence,
    }


async def _mark_source_reflected(
    source: RawMemory,
    *,
    pack: ReflectionPack,
    run_id: str,
    dry_run: bool,
) -> dict[str, Any]:
    result = {
        "source_id": source.id,
        "outcome": "reflected",
        "dry_run": dry_run,
        "candidate_count": len(pack.candidates),
        "persisted_count": pack.persisted_count,
    }
    if dry_run:
        return result
    metadata = {
        **source.metadata,
        "reflection_dream_processed_at": datetime.now(UTC).isoformat(),
        "reflection_dream_run_id": run_id,
        "reflection_dream_candidate_count": len(pack.candidates),
        "reflection_dream_persisted_count": pack.persisted_count,
    }
    await save_raw_memory(replace(source, metadata=metadata))
    return result


async def _mark_source_processed(
    source: RawMemory,
    *,
    run_id: str,
    dry_run: bool,
    outcome: str,
    reason: str,
) -> dict[str, Any]:
    result = {
        "source_id": source.id,
        "outcome": outcome,
        "reason": reason,
        "dry_run": dry_run,
    }
    if dry_run:
        return result
    metadata = {
        **source.metadata,
        "reflection_dream_processed_at": datetime.now(UTC).isoformat(),
        "reflection_dream_run_id": run_id,
        "reflection_dream_skip_reason": reason,
    }
    await save_raw_memory(replace(source, metadata=metadata))
    return result


async def _archive_dream_exception_candidate(
    *,
    candidate: RawMemory,
    decision_reason: str,
    exception_reasons: list[str],
    run_id: str,
) -> RawMemory:
    archived_at = datetime.now(UTC).isoformat()
    metadata = {
        **candidate.metadata,
        "review_state": "archived",
        "archived_at": archived_at,
        "archive_reason": decision_reason,
        "archive_reasons": list(exception_reasons),
        "autonomy_outcome": "exception",
        "autonomy_recommended_action": "route_to_review",
        "reflection_dream_run_id": run_id,
    }
    return await save_raw_memory(replace(candidate, review_state="archived", metadata=metadata))


async def _accessible_projects_for_source(
    *,
    group_id: str,
    source: RawMemory,
) -> set[str]:
    return await _resolve_accessible_projects(
        group_id=group_id,
        principal_id=source.principal_id,
    )


async def _accessible_projects_for_candidate(
    *,
    group_id: str,
    candidate: RawMemory,
) -> set[str]:
    return await _resolve_accessible_projects(
        group_id=group_id,
        principal_id=candidate.principal_id,
    )


async def _resolve_accessible_projects(
    *,
    group_id: str,
    principal_id: str | None,
) -> set[str]:
    if not principal_id:
        return set()
    try:
        project_ids = await resolve_accessible_project_graph_ids(
            user_id=principal_id,
            org_id=group_id,
        )
    except Exception as exc:
        log.warning(
            "reflection_dream_project_access_lookup_failed",
            group_id=group_id,
            principal_id=principal_id,
            error=str(exc),
        )
        return set()
    return {str(project_id) for project_id in project_ids or set()}


async def _log_dream_candidate_audit(
    *,
    candidate: RawMemory,
    group_id: str,
    run_id: str,
    dry_run: bool,
    preview_allowed: bool,
    decision_reason: str,
    outcome: str,
    recommended_action: str,
    memory_scope: str | None,
    scope_key: str | None,
    project: str | None,
    raw_source_ids: list[str],
    promoted_id: str | None,
    exception_reasons: list[str],
    review_state: str,
) -> None:
    action = (
        "memory.reflect.dream_promote"
        if outcome == ReflectionAutonomyOutcome.AUTO_PROMOTE.value
        else "memory.reflect.dream_review"
    )
    try:
        await log_memory_audit_event(
            action=action,
            user_id=candidate.principal_id,
            organization_id=group_id,
            request=None,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project,
            source_surface="reflection_dream_cycle",
            source_ids=[candidate.id, *raw_source_ids],
            derived_ids=[promoted_id] if promoted_id else [],
            policy_allowed=preview_allowed,
            policy_reason=decision_reason,
            details={
                "dry_run": dry_run,
                "exception_reasons": list(exception_reasons),
                "outcome": outcome,
                "recommended_action": recommended_action,
                "review_state": review_state,
                "run_id": run_id,
            },
        )
    except Exception as exc:
        log.warning(
            "reflection_dream_audit_failed",
            candidate_id=candidate.id,
            error=str(exc),
            exc_info=True,
        )


def _archiveable_exception(
    exception_reasons: list[str],
    *,
    archive_reasons: set[str],
) -> bool:
    reasons = {str(reason) for reason in exception_reasons if str(reason)}
    return bool(reasons & archive_reasons) and reasons <= archive_reasons


def _candidate_target_scope(candidate: RawMemory) -> str:
    return (
        _metadata_str(candidate.metadata, "suggested_memory_scope") or candidate.memory_scope.value
    )


def _candidate_target_scope_key(candidate: RawMemory, target_scope: str) -> str | None:
    if suggested_scope_key := _metadata_str(candidate.metadata, "suggested_scope_key"):
        return suggested_scope_key
    if candidate.memory_scope.value == target_scope:
        return candidate.scope_key
    if target_scope == MemoryScope.PROJECT.value:
        return candidate.project_id
    return None


def _candidate_project(
    candidate: RawMemory,
    *,
    target_scope: str,
    target_scope_key: str | None,
) -> str | None:
    return (
        candidate.project_id
        or _metadata_str(candidate.metadata, "project_id")
        or (target_scope_key if target_scope == MemoryScope.PROJECT.value else None)
    )


def _metadata_str(metadata: dict[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _metadata_str_list(value: object) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item) for item in value if str(item)]


def _summary_log_fields(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        key: receipt[key]
        for key in (
            "run_id",
            "group_id",
            "dry_run",
            "sources_scanned",
            "sources_reflected",
            "candidates_scanned",
            "promoted",
            "archived",
            "exceptioned",
            "skipped",
            "failed",
            "latency_ms",
        )
    }


async def _list_organization_ids() -> list[str]:
    from sibyl.persistence.organization_runtime import list_org_ids

    return await list_org_ids()


__all__ = ["run_reflection_dream_cycle", "run_reflection_dream_cycle_all_orgs"]
