"""Scheduled recovery of captures and graph rows awaiting source checks."""

import asyncio
from collections.abc import Awaitable, Mapping, Sequence
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
from sibyl_core.config import settings
from sibyl_core.embeddings.providers import configured_embedding_provider
from sibyl_core.projection.repair import LifecycleRepairResult, repair_graph_lifecycle
from sibyl_core.services import content_client, content_models, memory_embedding
from sibyl_core.services.content_raw_embedding_repair import (
    RAW_CAPTURE_EMBEDDING_PLANE,
    repair_raw_capture_embeddings,
)
from sibyl_core.services.document_embedding_sweep import (
    DOCUMENT_CHUNK_EMBEDDING_PLANE,
    ChunkEmbedder,
    content_sweep_schema_ready,
    sweep_document_chunk_embeddings,
)
from sibyl_core.services.embedding_evidence import (
    read_evidence_wait,
    read_published_organizations,
    record_evidence_wait,
)
from sibyl_core.services.embedding_sweep import (
    SWEEP_COMPLETED,
    SWEEP_CURRENT,
    SWEEP_PARTIAL,
    SWEEP_PROVIDER_FAILING,
    SWEEP_SKIPPED_DIMENSION_MISMATCH,
    SWEEP_SKIPPED_NO_PROVIDER,
    SWEEP_SKIPPED_SCHEMA_PENDING,
    SWEEP_STORE_FAILING,
    EmbeddingSchemaPendingError,
    EmbeddingSweepResult,
)
from sibyl_core.services.embedding_verdicts import (
    LegacyVerdicts,
    settle_legacy_verdicts,
    verdict_settled,
)
from sibyl_core.services.graph_embedding_sweep import (
    GRAPH_EMBEDDING_PLANE,
    sweep_graph_embeddings,
)
from sibyl_core.services.graph_runtime import background_graph_runtime
from sibyl_core.services.lifecycle_probe import (
    ContentWork,
    PlaneFacts,
    probe_content_work,
    probe_graph_work,
)
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
    "embedding_schema_pending",
)
# Pass outcomes that say the configured provider or store is not working, so
# the pass does not vouch for its configuration in the deployment record.
_UNHEALTHY_SWEEP_STATUSES = frozenset(
    {SWEEP_PROVIDER_FAILING, SWEEP_STORE_FAILING, SWEEP_SKIPPED_DIMENSION_MISMATCH}
)

_EXERCISED_SWEEP_STATUSES = frozenset({SWEEP_COMPLETED, SWEEP_CURRENT, SWEEP_PARTIAL})

type _ChunkInputs = tuple[dict[str, Any], bool, ChunkEmbedder]


async def _chunk_sweep_inputs() -> _ChunkInputs | BaseException:
    try:
        return await document_chunk_sweep_inputs()
    except Exception as exc:
        log.warning("embedding_sweep_chunk_inputs_failed", error_type=type(exc).__name__)
        return exc


async def _content_schema_ready() -> bool:
    """Whether the shared content namespace has taken the sweep's evidence snapshot.

    A worker can tick before the API has migrated the content schema. Until it
    has, nothing in this pass may read or rewrite embedding evidence: raw
    capture repair would restamp the captures the upgrade is about to
    photograph, and the chunk plane's bookkeeping does not exist yet.
    """
    try:
        async with content_client.surreal_content_client() as client:
            return await content_sweep_schema_ready(client)
    except Exception as exc:
        log.warning("embedding_sweep_schema_check_failed", error_type=type(exc).__name__)
        return False


async def _settle_verdicts(
    organization_id: str,
    runtime: Any,
    chunk_inputs: _ChunkInputs | BaseException,
    *,
    organizations: Sequence[str],
    allow_unproven: bool = False,
) -> LegacyVerdicts | BaseException:
    """Record both planes' verdicts on unstamped vectors before any pass runs.

    Each verdict weighs the other plane's pre-upgrade evidence, so both are
    settled together against the organization's graph namespace and the
    shared content namespace. The evidence was photographed when each schema
    upgraded, so the repairs that run alongside cannot disturb it. An
    adoption waits until every one of ``organizations`` has published, so a
    verdict never depends on which organization this pass reached first.
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
            defer_limit_seconds=settings.embedding_sweep_evidence_wait_seconds,
            deployment_organizations=organizations,
        )
    except EmbeddingSchemaPendingError as exc:
        return exc
    except Exception as exc:
        log.warning(
            "embedding_sweep_verdicts_failed",
            group_id=organization_id,
            error_type=type(exc).__name__,
        )
        return exc


async def _no_chunk_embedder(_rows: object) -> tuple[list[list[float]], dict[str, Any]]:
    raise RuntimeError("no chunk embedder is configured")


async def _graph_repairs(runtime: Any) -> list[object]:
    """Graph lifecycle and promoted-embedding repair, each failing on its own."""
    results: list[object] = []
    for repair in (repair_graph_lifecycle, repair_promoted_embeddings):
        try:
            results.append(await repair(runtime))
        except Exception as exc:
            results.append(exc)
    lifecycle, embeddings = results
    if isinstance(lifecycle, LifecycleRepairResult) and isinstance(
        embeddings, LifecycleRepairResult
    ):
        return [
            type(lifecycle)(
                **{key: value + asdict(embeddings)[key] for key, value in asdict(lifecycle).items()}
            )
        ]
    return results


async def _repair_graph(
    organization_id: str,
    chunk_inputs: _ChunkInputs | BaseException,
    *,
    sweeping: bool,
    organizations: Sequence[str],
    settled: LegacyVerdicts | BaseException | None = None,
) -> tuple[object, ...]:
    """Settle both verdicts, then run the graph repairs beside both sweeps.

    One background runtime serves the whole organization pass. A plane whose
    verdict could not be settled, or was deferred until every organization
    has published its evidence, skips its sweep this pass rather than
    deciding on partial evidence. The verdicts ride back in the result, even
    when a graph repair fails, so the caller can settle deferred planes.
    ``settled`` carries verdicts the tick settled already, in organization
    order, so they are not weighed again here. Without ``sweeping`` only the
    graph repairs run.
    """
    async with background_graph_runtime(organization_id) as runtime:
        reported: list[object] = []
        graph_settled = chunks_settled = False
        if sweeping:
            verdicts = (
                settled
                if settled is not None
                else await _settle_verdicts(
                    organization_id, runtime, chunk_inputs, organizations=organizations
                )
            )
            reported.append(verdicts)
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
            repaired = await _graph_repairs(runtime)
        finally:
            swept = await asyncio.gather(*sweeps, return_exceptions=True)
        return (*repaired, *swept, *reported)


async def _settle_unproven(organization_id: str, organizations: Sequence[str]) -> None:
    """Settle planes deferred for lack of evidence, now that every organization published.

    Their sweeps start on the next pass.
    """
    chunk_inputs = await _chunk_sweep_inputs()
    try:
        async with background_graph_runtime(organization_id) as runtime:
            await _settle_verdicts(
                organization_id,
                runtime,
                chunk_inputs,
                organizations=organizations,
                allow_unproven=True,
            )
    except Exception as exc:
        log.warning(
            "embedding_sweep_deferred_verdicts_failed",
            group_id=organization_id,
            error_type=type(exc).__name__,
        )


def _chunk_stamp(chunk_inputs: _ChunkInputs | BaseException | None) -> dict[str, Any] | None:
    """The stamp chunk writes carry today, or None when no chunk embedder can run."""
    if chunk_inputs is None or isinstance(chunk_inputs, BaseException):
        return None
    stamp, runnable, _embed = chunk_inputs
    return stamp if runnable else None


def _raw_stamp() -> dict[str, Any] | None:
    provider = content_models.configured_raw_memory_embedding_provider()
    if provider is None:
        return None
    return dict(content_models.raw_memory_embedding_metadata(provider.metadata))


async def _probe_content(chunk_inputs: _ChunkInputs | BaseException | None) -> ContentWork | None:
    """What every organization still owes on the content side, in one round trip.

    A failed probe is logged and answered with None, which sends every
    organization through its full pass as before.
    """
    stamps: dict[str, Mapping[str, Any]] = {}
    if (chunk_stamp := _chunk_stamp(chunk_inputs)) is not None:
        stamps[DOCUMENT_CHUNK_EMBEDDING_PLANE] = chunk_stamp
    if (raw_stamp := _raw_stamp()) is not None:
        stamps[RAW_CAPTURE_EMBEDDING_PLANE] = raw_stamp
    try:
        async with content_client.surreal_content_client() as client:
            return await probe_content_work(
                client,
                stamps=stamps,
                verify_interval=settings.embedding_sweep_verify_interval_seconds,
            )
    except Exception as exc:
        log.warning("lifecycle_repair_content_probe_failed", error_type=type(exc).__name__)
        return None


def _current_sweep(plane: str, facts: PlaneFacts | None) -> EmbeddingSweepResult:
    """The result a sweep returns for a plane it would skip as current."""
    if facts is None:
        return EmbeddingSweepResult(plane=plane, status=SWEEP_SKIPPED_NO_PROVIDER)
    return EmbeddingSweepResult(
        plane=plane,
        status=SWEEP_CURRENT,
        legacy_decision=facts.legacy_decision,
        warning=facts.warning,
        notice=facts.notice,
    )


async def _idle_outcomes(
    organization_id: str,
    chunk_inputs: _ChunkInputs | BaseException | None,
    content_work: ContentWork,
) -> list[object] | None:
    """The outcomes of an organization that owes nothing, or None when it owes something.

    The content answer was read for every organization at once, so it is
    checked first; only an organization idle there pays the graph probe. A
    failed probe answers None, which runs the full pass as before. The
    outcomes are what the sweeps would have reported: current, with the
    verdict each plane holds, so the tick's summary reads the same.
    """
    chunk_stamp = _chunk_stamp(chunk_inputs)
    planes = [
        plane
        for plane, stamp in (
            (DOCUMENT_CHUNK_EMBEDDING_PLANE, chunk_stamp),
            (RAW_CAPTURE_EMBEDDING_PLANE, _raw_stamp()),
        )
        if stamp is not None
    ]
    if not content_work.idle(organization_id, planes=planes):
        return None
    provider = configured_embedding_provider()
    # The promoted-embedding repair resolves its provider itself; the probe
    # stands in for that repair, so it asks the same source.
    promoted = memory_embedding.configured_embedding_provider()
    try:
        async with background_graph_runtime(organization_id) as runtime:
            work = await probe_graph_work(
                runtime.client,
                graph_stamp=provider.metadata.to_dict() if provider is not None else None,
                embedding_stamp=promoted.metadata.to_dict() if promoted is not None else None,
                verify_interval=settings.embedding_sweep_verify_interval_seconds,
            )
    except Exception as exc:
        log.warning(
            "lifecycle_repair_graph_probe_failed",
            group_id=organization_id,
            error_type=type(exc).__name__,
        )
        return None
    if not work.idle:
        return None
    outcomes: list[object] = [chunk_inputs] if isinstance(chunk_inputs, BaseException) else []
    outcomes.append(_current_sweep(GRAPH_EMBEDDING_PLANE, work.sweep))
    outcomes.append(
        _current_sweep(
            DOCUMENT_CHUNK_EMBEDDING_PLANE,
            content_work.facts(organization_id, DOCUMENT_CHUNK_EMBEDDING_PLANE)
            if chunk_stamp is not None
            else None,
        )
    )
    return outcomes


async def _settle_organization(
    organization_id: str,
    chunk_inputs: _ChunkInputs | BaseException,
    organizations: Sequence[str],
) -> LegacyVerdicts | BaseException | None:
    """Settle one organization's verdicts ahead of its pass.

    None when the organization's graph cannot be reached; the pass itself
    then reports that failure the way it always has.
    """
    try:
        async with background_graph_runtime(organization_id) as runtime:
            return await _settle_verdicts(
                organization_id, runtime, chunk_inputs, organizations=organizations
            )
    except Exception:
        return None


async def _repair_organization(
    organization_id: str,
    *,
    sweeping: bool = True,
    organizations: Sequence[str] = (),
    chunk_inputs: _ChunkInputs | BaseException | None = None,
    settled: LegacyVerdicts | BaseException | None = None,
) -> list[object]:
    """Run every repair for one organization, isolating each one's failure.

    Without ``sweeping`` (the content schema has not upgraded yet) nothing
    that reads or rewrites embedding evidence runs. ``settled`` carries the
    verdicts the tick settled for this organization already.
    """
    if sweeping and chunk_inputs is None:
        chunk_inputs = await _chunk_sweep_inputs()
    repairs: list[Awaitable[object]] = [
        _repair_graph(
            organization_id,
            chunk_inputs if chunk_inputs is not None else RuntimeError("not sweeping"),
            sweeping=sweeping,
            organizations=organizations or [organization_id],
            settled=settled,
        ),
        repair_raw_source_lifecycle(organization_id, authority_resolver=resolve_source_authority),
    ]
    if sweeping:
        repairs.append(repair_raw_capture_embeddings(organization_id))
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
    summary["embedding_schema_pending"] += 1 if sweep.status == SWEEP_SKIPPED_SCHEMA_PENDING else 0
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


async def _settle_deferred(
    organizations: list[str],
    deferred: list[str],
    planes: int,
    *,
    oldest_wait_seconds: float | None = None,
) -> None:
    """Settle deferred planes once every organization has published, and say who is missing.

    Publication is persistent, so an organization counts once it has
    published in any pass. While some have not, the deferred planes keep
    waiting (each for at most the configured evidence wait, enforced when it
    is next weighed) and the missing organizations are logged and recorded
    for status.
    """
    async with content_client.surreal_content_client() as client:

        async def execute(query: str, **params: object) -> object:
            return await content_client.select_many(client, query, **params)

        published = await read_published_organizations(execute)
        waiting_on = [
            organization for organization in organizations if organization not in published
        ]
        if deferred or (await read_evidence_wait(execute)).get("count"):
            await record_evidence_wait(
                execute, waiting_on=waiting_on if deferred else [], deferred_planes=planes
            )
    if not deferred:
        return
    if waiting_on:
        bound = settings.embedding_sweep_evidence_wait_seconds
        log.warning(
            "embedding_evidence_waiting_on_organizations",
            deferred_planes=planes,
            waiting_on=len(waiting_on),
            organizations=waiting_on[:10],
            wait_seconds=bound,
            # The longest-waiting plane settles on the evidence published so
            # far within this many seconds.
            settles_within_seconds=(
                max(0, round(bound - oldest_wait_seconds))
                if oldest_wait_seconds is not None
                else None
            ),
        )
        return
    for organization_id in deferred:
        await _settle_unproven(organization_id, organizations)


async def _visit_organizations(
    organizations: Sequence[str],
    *,
    sweeping: bool,
    chunk_inputs: _ChunkInputs | BaseException | None,
    content_work: ContentWork | None,
) -> list[list[object]]:
    """Every organization's outcomes: probed in parallel, settled in order, repaired in parallel."""
    slots = asyncio.Semaphore(settings.lifecycle_repair_concurrency)

    async def probe(organization_id: str) -> list[object] | None:
        if not sweeping or content_work is None or chunk_inputs is None:
            return None
        async with slots:
            return await _idle_outcomes(organization_id, chunk_inputs, content_work)

    idle = dict(
        zip(
            organizations,
            await asyncio.gather(*(probe(organization_id) for organization_id in organizations)),
            strict=True,
        )
    )
    # Verdicts settle in organization order, one at a time: a plane waits
    # for the organizations listed after it to publish their evidence, and
    # that wait is what keeps a verdict from depending on who went first
    # (see embedding_verdicts). The sweeps and repairs then overlap.
    settled: dict[str, LegacyVerdicts | BaseException | None] = {}
    if sweeping and chunk_inputs is not None:
        for organization_id in organizations:
            if idle[organization_id] is None:
                settled[organization_id] = await _settle_organization(
                    organization_id, chunk_inputs, organizations
                )

    async def visit(organization_id: str) -> list[object]:
        outcomes = idle[organization_id]
        if outcomes is not None:
            return outcomes
        async with slots:
            return await _repair_organization(
                organization_id,
                sweeping=sweeping,
                organizations=organizations,
                chunk_inputs=chunk_inputs,
                settled=settled.get(organization_id),
            )

    return await asyncio.gather(*(visit(organization_id) for organization_id in organizations))


async def repair_lifecycle_all_orgs(ctx: dict[str, Any]) -> dict[str, int]:  # noqa: ARG001
    summary = {
        "organizations": 0,
        "failed_organizations": 0,
        "checked": 0,
        "recovered": 0,
        "pending": 0,
        "failed": 0,
        # Raw captures the provider refused, or the model kept failing on,
        # stored and skipped by later passes.
        "refused": 0,
        "deferred": 0,
        **dict.fromkeys(_EMBEDDING_SUMMARY_KEYS, 0),
    }
    sweeping = await _content_schema_ready()
    if not sweeping:
        log.info("embedding_sweep_waiting_for_content_schema")
    organizations = await list_org_ids()
    # The chunk stamp and the content-side answer are deployment-wide, so
    # both are read once per tick rather than once per organization.
    chunk_inputs = await _chunk_sweep_inputs() if sweeping else None
    content_work = await _probe_content(chunk_inputs) if sweeping else None
    repaired = await _visit_organizations(
        organizations, sweeping=sweeping, chunk_inputs=chunk_inputs, content_work=content_work
    )
    deferred: list[str] = []
    oldest_wait: float | None = None
    exercised = unhealthy = False
    for organization_id, results in zip(organizations, repaired, strict=True):
        summary["organizations"] += 1
        outcomes: list[object] = []
        for result in results:
            if isinstance(result, tuple):
                outcomes.extend(result)
            else:
                outcomes.append(result)
        if not sweeping:
            summary["embedding_schema_pending"] += 2
        verdicts = next((item for item in outcomes if isinstance(item, LegacyVerdicts)), None)
        if verdicts is not None and verdicts.deferred:
            deferred.append(organization_id)
            age = verdicts.deferred_age_seconds
            if age is not None:
                oldest_wait = age if oldest_wait is None else max(oldest_wait, age)
            summary["embedding_deferred"] += sum(
                1
                for verdict in (verdicts.graph, verdicts.document_chunks)
                if not verdict_settled(verdict) and not isinstance(verdict, BaseException)
            )
        failures = [
            outcome
            for outcome in outcomes
            if isinstance(outcome, BaseException)
            and not isinstance(outcome, EmbeddingSchemaPendingError)
        ]
        if failures:
            summary["failed_organizations"] += 1
        for outcome in outcomes:
            if isinstance(outcome, EmbeddingSchemaPendingError):
                summary["embedding_schema_pending"] += 1
            elif isinstance(outcome, BaseException):
                log.warning(
                    "lifecycle_repair_org_failed",
                    group_id=organization_id,
                    error_type=type(outcome).__name__,
                )
            elif isinstance(outcome, EmbeddingSweepResult):
                _record_embedding_sweep(summary, organization_id, outcome)
                exercised = exercised or outcome.status in _EXERCISED_SWEEP_STATUSES
                # A provider that refused every row it was sent is not a
                # configuration to vouch for, even short of the outage breaker.
                unhealthy = unhealthy or (
                    outcome.status in _UNHEALTHY_SWEEP_STATUSES
                    or (outcome.failed > 0 and outcome.recovered == 0)
                )
            elif isinstance(outcome, LegacyVerdicts):
                continue
            else:
                for key, value in asdict(outcome).items():
                    if key in summary and isinstance(value, int):
                        summary[key] += value
    if sweeping:
        try:
            await _settle_deferred(
                organizations,
                deferred,
                summary["embedding_deferred"],
                oldest_wait_seconds=oldest_wait,
            )
        except Exception as exc:
            log.warning("embedding_deferred_settle_failed", error_type=type(exc).__name__)
    # The deployment record vouches only for a configuration a pass actually
    # swept under without the provider or store failing, so a process that
    # merely started with a wrong configuration leaves no mark.
    if sweeping and exercised and not unhealthy:
        await record_configured_embedding_models()
    log.info("lifecycle_repair_completed", **summary)
    return summary
