"""Scheduled recovery of captures and graph rows awaiting source checks."""

import asyncio
from collections.abc import Awaitable
from dataclasses import asdict
from typing import Any

import structlog

from sibyl.jobs.embedding_sweep import document_chunk_sweep_inputs
from sibyl.persistence.auth_common import InvalidAuthClaimsError, UserNotFoundError
from sibyl.persistence.auth_runtime import (
    list_accessible_delegated_scope_keys,
    list_accessible_project_graph_ids,
    list_accessible_team_scope_keys,
    resolve_auth_context,
)
from sibyl.persistence.organization_runtime import list_org_ids
from sibyl_core.projection.repair import LifecycleRepairResult, repair_graph_lifecycle
from sibyl_core.services import content_client
from sibyl_core.services.content_raw_embedding_repair import repair_raw_capture_embeddings
from sibyl_core.services.document_embedding_sweep import (
    DOCUMENT_CHUNK_EMBEDDING_PLANE,
    ChunkEmbedder,
    decide_document_chunk_legacy_vectors,
    sweep_document_chunk_embeddings,
)
from sibyl_core.services.embedding_sweep import (
    SWEEP_CURRENT,
    SWEEP_SKIPPED_NO_PROVIDER,
    EmbeddingSweepResult,
    read_embedding_sweep_state,
)
from sibyl_core.services.graph_embedding_sweep import (
    GRAPH_EMBEDDING_PLANE,
    decide_graph_legacy_vectors,
    sweep_graph_embeddings,
)
from sibyl_core.services.graph_runtime import background_graph_runtime
from sibyl_core.services.memory_embedding import repair_promoted_embeddings
from sibyl_core.services.memory_source_validation import (
    SourceReadAuthority,
    repair_raw_source_lifecycle,
)

log = structlog.get_logger()


async def resolve_source_authority(
    organization_id: str, principal_id: str
) -> SourceReadAuthority | None:
    """Continue an accepted capture using current membership and its saved ceiling."""
    try:
        context = await resolve_auth_context(claims={"sub": principal_id, "org": organization_id})
    except (InvalidAuthClaimsError, UserNotFoundError):
        return None
    if (
        context.user_id != principal_id
        or context.organization_id != organization_id
        or context.org_role is None
    ):
        return None
    projects, teams, delegations = await asyncio.gather(
        list_accessible_project_graph_ids(context),
        list_accessible_team_scope_keys(context),
        list_accessible_delegated_scope_keys(context),
    )
    return SourceReadAuthority(
        principal_id=principal_id,
        projects=frozenset(projects),
        teams=frozenset(teams),
        delegations=frozenset(delegations),
    )


_EMBEDDING_SUMMARY_KEYS = (
    "embedding_checked",
    "embedding_reembedded",
    "embedding_adopted",
    "embedding_pending",
    "embedding_skipped",
    "embedding_rejected",
    "embedding_failed",
)


async def _repair_graph(
    organization_id: str,
) -> tuple[LifecycleRepairResult, EmbeddingSweepResult | BaseException]:
    async with background_graph_runtime(organization_id) as runtime:
        # The sweep's verdict on unstamped vectors reads stamps that the
        # promoted-embedding repair can rewrite, so it is settled first.
        sweep_task: asyncio.Task[EmbeddingSweepResult] | None = None
        sweep: EmbeddingSweepResult | BaseException
        restamp_allowed = True
        try:
            await decide_graph_legacy_vectors(runtime)
        except Exception as exc:
            sweep = exc
            restamp_allowed = await _graph_verdict_recorded(runtime, organization_id)
        else:
            sweep_task = asyncio.create_task(sweep_graph_embeddings(runtime))
        try:
            lifecycle = await repair_graph_lifecycle(runtime)
            embeddings = (
                await repair_promoted_embeddings(runtime)
                if restamp_allowed
                else LifecycleRepairResult()
            )
        finally:
            if sweep_task is not None:
                try:
                    sweep = await sweep_task
                except Exception as exc:
                    sweep = exc
        combined = type(lifecycle)(
            **{key: value + asdict(embeddings)[key] for key, value in asdict(lifecycle).items()}
        )
        return combined, sweep


type _ChunkPlane = tuple[dict[str, Any], bool, ChunkEmbedder]


async def _settle_chunk_verdict(organization_id: str) -> _ChunkPlane | BaseException:
    """Record the chunk plane's verdict, or return why it could not be settled.

    The verdict reads raw capture stamps, which the raw embedding repair
    rewrites after a provider switch, so it must be settled before that
    repair runs in the same pass.
    """
    try:
        stamp, runnable, embed_chunks = await document_chunk_sweep_inputs()
        await decide_document_chunk_legacy_vectors(
            organization_id, stamp=stamp, embed_chunks=embed_chunks
        )
    except Exception as exc:
        log.warning(
            "embedding_sweep_chunk_verdict_failed",
            group_id=organization_id,
            error_type=type(exc).__name__,
        )
        return exc
    return stamp, runnable, embed_chunks


async def _graph_verdict_recorded(runtime: Any, organization_id: str) -> bool:
    """Whether an earlier pass already persisted this organization's graph verdict."""
    try:
        state = await read_embedding_sweep_state(
            GRAPH_EMBEDDING_PLANE, organization_id, runtime.client.execute_query
        )
    except Exception:
        return False
    return bool(state.get("legacy_decision"))


async def _chunk_verdict_recorded(organization_id: str) -> bool:
    """Whether an earlier pass already persisted this organization's chunk verdict."""
    try:
        async with content_client.surreal_content_client() as client:
            state = await read_embedding_sweep_state(
                DOCUMENT_CHUNK_EMBEDDING_PLANE,
                organization_id,
                lambda query, **params: content_client.select_many(client, query, **params),
            )
    except Exception:
        return False
    return bool(state.get("legacy_decision"))


async def _repair_organization(organization_id: str) -> list[object]:
    """Run every repair for one organization, isolating each one's failure."""
    chunk_plane = await _settle_chunk_verdict(organization_id)
    repairs: list[Awaitable[object]] = [
        _repair_graph(organization_id),
        repair_raw_source_lifecycle(organization_id, authority_resolver=resolve_source_authority),
    ]
    outcomes: list[object] = []
    if isinstance(chunk_plane, BaseException):
        outcomes.append(chunk_plane)
        # Until a verdict exists, restamping raw captures would erase the
        # evidence it needs; once one is recorded the raw repair is free to run.
        if await _chunk_verdict_recorded(organization_id):
            repairs.append(repair_raw_capture_embeddings(organization_id))
    else:
        stamp, runnable, embed_chunks = chunk_plane
        repairs.append(repair_raw_capture_embeddings(organization_id))
        repairs.append(
            sweep_document_chunk_embeddings(
                organization_id, stamp=stamp if runnable else None, embed_chunks=embed_chunks
            )
        )
    outcomes.extend(await asyncio.gather(*repairs, return_exceptions=True))
    return outcomes


def _record_embedding_sweep(
    summary: dict[str, int], organization_id: str, sweep: EmbeddingSweepResult
) -> None:
    summary["embedding_checked"] += sweep.checked
    summary["embedding_reembedded"] += sweep.recovered
    summary["embedding_adopted"] += sweep.adopted
    summary["embedding_pending"] += sweep.pending
    summary["embedding_skipped"] += sweep.skipped
    summary["embedding_rejected"] += sweep.rejected
    summary["embedding_failed"] += sweep.failed
    if sweep.status in {SWEEP_CURRENT, SWEEP_SKIPPED_NO_PROVIDER}:
        return
    log.info(
        "lifecycle_embedding_sweep",
        group_id=organization_id,
        plane=sweep.plane,
        status=sweep.status,
        checked=sweep.checked,
        reembedded=sweep.recovered,
        adopted=sweep.adopted,
        pending=sweep.pending,
        skipped=sweep.skipped,
        rejected=sweep.rejected,
        failed=sweep.failed,
    )


async def repair_lifecycle_all_orgs(ctx: dict[str, Any]) -> dict[str, int]:  # noqa: ARG001
    summary = {
        "organizations": 0,
        "failed_organizations": 0,
        "checked": 0,
        "recovered": 0,
        "pending": 0,
        "failed": 0,
        **dict.fromkeys(_EMBEDDING_SUMMARY_KEYS, 0),
    }
    for organization_id in await list_org_ids():
        summary["organizations"] += 1
        outcomes: list[object] = []
        for result in await _repair_organization(organization_id):
            if isinstance(result, tuple):
                outcomes.extend(result)
            else:
                outcomes.append(result)
        if any(isinstance(outcome, BaseException) for outcome in outcomes):
            summary["failed_organizations"] += 1
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                log.warning(
                    "lifecycle_repair_org_failed",
                    group_id=organization_id,
                    error_type=type(outcome).__name__,
                )
            elif isinstance(outcome, EmbeddingSweepResult):
                _record_embedding_sweep(summary, organization_id, outcome)
            else:
                for key, value in asdict(outcome).items():
                    if key in summary and isinstance(value, int):
                        summary[key] += value
    log.info("lifecycle_repair_completed", **summary)
    return summary
