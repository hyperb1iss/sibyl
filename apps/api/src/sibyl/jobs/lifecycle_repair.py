"""Scheduled recovery of captures and graph rows awaiting source checks."""

import asyncio
from collections.abc import Awaitable
from dataclasses import asdict
from typing import Any

import structlog

from sibyl.jobs.embedding_sweep import (
    document_chunk_sweep_inputs,
    record_configured_embedding_models,
)
from sibyl.persistence.auth_common import InvalidAuthClaimsError, UserNotFoundError
from sibyl.persistence.auth_runtime import (
    list_accessible_delegated_scope_keys,
    list_accessible_project_graph_ids,
    list_accessible_team_scope_keys,
    resolve_auth_context,
)
from sibyl.persistence.organization_runtime import list_org_ids
from sibyl_core.embeddings.providers import configured_embedding_provider
from sibyl_core.projection.repair import repair_graph_lifecycle
from sibyl_core.services.content_raw_embedding_repair import repair_raw_capture_embeddings
from sibyl_core.services.document_embedding_sweep import (
    ChunkEmbedder,
    sweep_document_chunk_embeddings,
)
from sibyl_core.services.embedding_sweep import (
    SWEEP_CURRENT,
    SWEEP_SKIPPED_NO_PROVIDER,
    EmbeddingSweepResult,
)
from sibyl_core.services.embedding_verdicts import (
    LegacyVerdicts,
    settle_legacy_verdicts,
    verdict_settled,
)
from sibyl_core.services.graph_embedding_sweep import sweep_graph_embeddings
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
    "embedding_unverified",
    "embedding_deferred",
)

type _ChunkInputs = tuple[dict[str, Any], bool, ChunkEmbedder]


async def _chunk_sweep_inputs() -> _ChunkInputs | BaseException:
    try:
        return await document_chunk_sweep_inputs()
    except Exception as exc:
        log.warning("embedding_sweep_chunk_inputs_failed", error_type=type(exc).__name__)
        return exc


async def _settle_verdicts(
    organization_id: str,
    runtime: Any,
    chunk_inputs: _ChunkInputs | BaseException,
    *,
    allow_unproven: bool = False,
) -> LegacyVerdicts | BaseException:
    """Record both planes' verdicts on unstamped vectors before any pass runs.

    Each verdict weighs the other plane's pre-upgrade evidence, so both are
    settled together against the organization's graph namespace and the
    shared content namespace. The evidence was photographed when each schema
    upgraded, so the repairs that run alongside cannot disturb it.
    """
    chunk_stamp: dict[str, Any] | None = None
    embed_chunks: ChunkEmbedder = _no_chunk_embedder
    if not isinstance(chunk_inputs, BaseException):
        chunk_stamp, _runnable, embed_chunks = chunk_inputs
    try:
        return await settle_legacy_verdicts(
            organization_id,
            graph_client=runtime.client,
            graph_provider=configured_embedding_provider(),
            chunk_stamp=chunk_stamp,
            embed_chunks=embed_chunks,
            allow_unproven=allow_unproven,
        )
    except Exception as exc:
        log.warning(
            "embedding_sweep_verdicts_failed",
            group_id=organization_id,
            error_type=type(exc).__name__,
        )
        return exc


async def _no_chunk_embedder(_rows: object) -> tuple[list[list[float]], dict[str, Any]]:
    raise RuntimeError("no chunk embedder is configured")


async def _repair_graph(
    organization_id: str, chunk_inputs: _ChunkInputs | BaseException
) -> tuple[object, ...]:
    """Settle both verdicts, then run the graph repairs beside both sweeps.

    One background runtime serves the whole organization pass. A plane whose
    verdict could not be settled, or was deferred until every organization
    has published its evidence, skips its sweep this pass rather than
    deciding on partial evidence. The verdicts ride back in the result so the
    caller can settle deferred planes.
    """
    async with background_graph_runtime(organization_id) as runtime:
        verdicts = await _settle_verdicts(organization_id, runtime, chunk_inputs)
        reported: list[object] = [verdicts]
        graph_settled = chunks_settled = False
        if isinstance(verdicts, LegacyVerdicts):
            graph_settled = verdict_settled(verdicts.graph)
            chunks_settled = verdict_settled(verdicts.document_chunks)
            reported.extend(
                verdict
                for verdict in (verdicts.graph, verdicts.document_chunks)
                if isinstance(verdict, BaseException)
            )
        sweeps: list[asyncio.Task[EmbeddingSweepResult]] = []
        if graph_settled:
            sweeps.append(asyncio.create_task(sweep_graph_embeddings(runtime)))
        if chunks_settled and not isinstance(chunk_inputs, BaseException):
            stamp, runnable, embed_chunks = chunk_inputs
            sweeps.append(
                asyncio.create_task(
                    sweep_document_chunk_embeddings(
                        organization_id,
                        stamp=stamp if runnable else None,
                        embed_chunks=embed_chunks,
                    )
                )
            )
        try:
            lifecycle = await repair_graph_lifecycle(runtime)
            embeddings = await repair_promoted_embeddings(runtime)
        finally:
            swept = await asyncio.gather(*sweeps, return_exceptions=True)
        combined = type(lifecycle)(
            **{key: value + asdict(embeddings)[key] for key, value in asdict(lifecycle).items()}
        )
        return (combined, *swept, *reported)


async def _settle_unproven(organization_id: str) -> None:
    """Settle planes deferred for lack of evidence, now that every organization published.

    Their sweeps start on the next pass.
    """
    chunk_inputs = await _chunk_sweep_inputs()
    try:
        async with background_graph_runtime(organization_id) as runtime:
            await _settle_verdicts(organization_id, runtime, chunk_inputs, allow_unproven=True)
    except Exception as exc:
        log.warning(
            "embedding_sweep_deferred_verdicts_failed",
            group_id=organization_id,
            error_type=type(exc).__name__,
        )


async def _repair_organization(organization_id: str) -> list[object]:
    """Run every repair for one organization, isolating each one's failure."""
    chunk_inputs = await _chunk_sweep_inputs()
    repairs: list[Awaitable[object]] = [
        _repair_graph(organization_id, chunk_inputs),
        repair_raw_source_lifecycle(organization_id, authority_resolver=resolve_source_authority),
        repair_raw_capture_embeddings(organization_id),
    ]
    outcomes: list[object] = [chunk_inputs] if isinstance(chunk_inputs, BaseException) else []
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
    summary["embedding_unverified"] += 1 if sweep.warning else 0
    if sweep.status in {SWEEP_CURRENT, SWEEP_SKIPPED_NO_PROVIDER}:
        return
    log.info(
        "lifecycle_embedding_sweep",
        group_id=organization_id,
        plane=sweep.plane,
        status=sweep.status,
        warning=sweep.warning,
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
    # Startup records the configured models too; refreshing here keeps the
    # record current for a process whose startup write failed.
    await record_configured_embedding_models()
    deferred: list[str] = []
    every_organization_published = True
    for organization_id in await list_org_ids():
        summary["organizations"] += 1
        outcomes: list[object] = []
        for result in await _repair_organization(organization_id):
            if isinstance(result, tuple):
                outcomes.extend(result)
            else:
                outcomes.append(result)
        verdicts = next((item for item in outcomes if isinstance(item, LegacyVerdicts)), None)
        if verdicts is None:
            # This organization's graph photograph may not have been published.
            every_organization_published = False
        elif verdicts.deferred:
            deferred.append(organization_id)
            summary["embedding_deferred"] += sum(
                1
                for verdict in (verdicts.graph, verdicts.document_chunks)
                if not verdict_settled(verdict) and not isinstance(verdict, BaseException)
            )
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
            elif isinstance(outcome, LegacyVerdicts):
                continue
            else:
                for key, value in asdict(outcome).items():
                    if key in summary and isinstance(value, int):
                        summary[key] += value
    # Planes with no evidence of their own wait until every organization's
    # graph has spoken, so the verdict never depends on which went first.
    if deferred and every_organization_published:
        for organization_id in deferred:
            await _settle_unproven(organization_id)
    log.info("lifecycle_repair_completed", **summary)
    return summary
