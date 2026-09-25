"""Reflection maintenance jobs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

import structlog

from sibyl.persistence.auth_common import InvalidAuthClaimsError, UserNotFoundError
from sibyl.persistence.auth_runtime import (
    log_memory_audit_event,
    resolve_accessible_project_graph_ids,
)
from sibyl_core.ai.errors import provider_error_detail
from sibyl_core.ai.llm.budget import (
    RUN_TOKEN_CEILING,
    budget_failure_fields,
    llm_budget_context,
    llm_run_spend_ledger,
    run_ceiling_reached,
)
from sibyl_core.auth import ProjectRole
from sibyl_core.config import settings
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.models.reflection import ReflectionPack
from sibyl_core.services.dream_checkpoints import (
    CheckpointReflectionExtractor,
    DreamSourceWork,
    SourceObservationKey,
    advance_dream_cursor,
    complete_dream_stage,
    completed_dream_sources,
    current_source_observations,
    load_dream_cursor,
    load_dream_stage,
)
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
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.observed_sources import load_authorized_source_snapshot
from sibyl_core.services.ordinary_cohort import ReflectedSources, reflected_sources
from sibyl_core.services.source_observations import SourceUnavailableError, observe_raw_capture
from sibyl_core.services.source_state_store import RawSourceSnapshot
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    list_reflection_candidate_reviews,
    list_reflection_dream_neighbours,
    list_reflection_dream_source_memories,
    save_raw_memory,
)
from sibyl_core.tools.reflect import reflect_memory

log = structlog.get_logger()


async def run_reflection_dream_cycle_all_orgs(
    ctx: dict[str, Any],
    *,
    dry_run: bool = False,
    source_limit: int = 20,
    candidate_limit: int = 50,
    archive_exceptions: bool = True,  # noqa: ARG001 - retained queued-job compatibility
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
    archive_exceptions: bool = True,  # noqa: ARG001 - retained queued-job compatibility
    archive_exception_reasons: list[str] | None = None,  # noqa: ARG001 - queued-job compatibility
    confidence_threshold: float | None = None,
) -> dict[str, Any]:
    started = datetime.now(UTC)
    start_time = perf_counter()
    run_id = f"reflection_dream:{group_id}:{uuid4()}"
    source_budget = max(0, min(source_limit, 100))
    candidate_budget = max(0, min(candidate_limit, 200))

    log.info(
        "reflection_dream_cycle_started",
        group_id=group_id,
        dry_run=dry_run,
        source_limit=source_budget,
        candidate_limit=candidate_budget,
        run_id=run_id,
    )

    # Every model call in the run reserves against this ledger before it
    # touches the monthly buckets, so one nightly run has a ceiling of its own.
    with llm_run_spend_ledger(settings.consolidation_run_max_tokens) as spend:
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
            confidence_threshold=confidence_threshold,
        )
    if spend.stopped_reason is not None:
        log.warning(
            "reflection_dream_run_ceiling_reached",
            group_id=group_id,
            run_id=run_id,
            **spend.snapshot(),
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
        "sources_scanned": len(
            {
                identifier
                for item in source_results
                for identifier in item.get("source_ids", [item.get("source_id")])
                if identifier is not None
            }
        ),
        "sources_reflected": sum(1 for item in source_results if item["outcome"] == "reflected"),
        "candidates_scanned": len(candidate_results),
        "promoted": sum(1 for item in candidate_results if item.get("applied") is True),
        "archived": sum(1 for item in candidate_results if item.get("archived") is True),
        "superseded_retired": sum(
            len(item.get("superseded_retired") or ()) for item in candidate_results
        ),
        "exceptioned": sum(1 for item in candidate_results if item["outcome"] == "exception"),
        "skipped": sum(1 for item in all_results if item["outcome"] == "skip"),
        "failed": sum(1 for item in all_results if item["outcome"] == "error"),
        "budget_refused": _budget_refusals(all_results),
        "spend": spend.snapshot(),
        "stopped_reason": spend.stopped_reason,
        "model_usage": {
            "accounting": "durable_validation_stages",
            "execution_ids": sorted(
                {
                    str(identifier)
                    for item in all_results
                    for identifier in _result_execution_ids(item)
                    if identifier is not None
                }
            ),
        },
        "sources": source_results,
        "candidates": candidate_results,
    }
    log.info("reflection_dream_cycle_completed", **_summary_log_fields(receipt))
    return receipt


def _budget_refusals(results: list[dict[str, Any]]) -> int:
    """Count budget refusals, including those on individual packet pages."""
    return sum(
        int(item.get("budget") is not None)
        + sum(1 for page in item.get("pages", []) if page.get("budget") is not None)
        for item in results
    )


def _result_execution_ids(item: dict[str, Any]) -> list[str | None]:
    if item.get("stage_kind") == "ordinary_cohort":
        return [item.get("operation_id")]
    if item.get("stage_kind") == "ordinary_packet_manifest":
        return [page.get("operation_id") for page in item.get("pages", [])]
    return item.get("validation_executions", [])


async def _reflect_dream_sources(
    *,
    group_id: str,
    run_id: str,
    dry_run: bool,
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    after_source_id, cursor_revision = await load_dream_cursor(group_id)
    cursor_owned = not dry_run
    selection = await _DreamSelection.load(group_id, limit)
    selected, selection_errors = selection.selected, selection.errors
    walk = await list_reflection_dream_source_memories(
        organization_id=group_id,
        limit=limit,
        is_pending=selection.fresh,
        after_source_id=after_source_id,
        prefetch=selection.prefetch,
    )
    sources, walked = await _dream_page(
        group_id, walk, limit, selection.neighbour, selection.prefetch
    )
    log.info(
        "reflection_dream_page_selected",
        group_id=group_id,
        run_id=run_id,
        seed_id=sources[0].id if sources else None,
        sources=len(sources),
        walked=len(walked),
        observation_misses=len(selection.unobserved),
        unsendable=selection.unsendable,
    )
    from sibyl.jobs.ordinary_cohorts import reflect_cohorts

    results, consumed = await reflect_cohorts(group_id, sources, dry_run=dry_run)
    for source in sources:
        if cursor_owned and source.id in walked:
            cursor_owned = await advance_dream_cursor(group_id, source.id, cursor_revision)
            cursor_revision += int(cursor_owned)
        if source.id in consumed:
            continue
        # Individual passes use the heuristic writer and make no model call, so
        # the run's token ceiling never stops them.
        try:
            if source.id in selection_errors:
                raise selection_errors[source.id]
            work = selected.get(source.id)
            if work is not None:
                stage = await load_dream_stage(work)
                if stage is not None and stage.get("completion_json") is not None:
                    continue
            results.append(
                await _reflect_dream_source(
                    source=source,
                    work=selected.get(source.id),
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
                    "provider_error": provider_error_detail(exc),
                    **budget_failure_fields(exc),
                }
            )
    return results


@dataclass
class _DreamSelection:
    """One run's view of what a pass already reflected, and the work it selected.

    A covered source bound into a completed cohort never returns. A source
    reflected alone, by an individual pass or every page of a packet
    manifest, never seeds a page or fills it from the walk, but may come back
    as a neighbour of a fresh seed, where a family member that arrived later
    can meet it. Current observations are read in batches before anything is
    authorized, so a reflected source is ruled out without authorizing it.

    Selection applies the provider-send gate as well as the read gate. A
    source its owner may still read but not send, such as a departed member's
    private captures or a project where they are now a viewer, fails every
    cohort preparation and never completes, so selecting it would only take a
    place on every page. It is unavailable until its owner's access returns.
    """

    group_id: str
    reflected: ReflectedSources
    paged: frozenset[SourceObservationKey]
    #: Sources reflected alone that one page may still admit, at most half
    #: of it, so a crowd of them near every seed cannot keep fresh sources off.
    returning: int
    selected: dict[str, DreamSourceWork] = field(default_factory=dict)
    errors: dict[str, Exception] = field(default_factory=dict)
    authorized: dict[str, str] = field(default_factory=dict)
    observed: dict[str, tuple[str, int]] = field(default_factory=dict)
    #: Each principal's send authority, resolved once per run; None when the
    #: send gate refuses them outright.
    send_authorities: dict[str, SourceReadAuthority | None] = field(default_factory=dict)
    #: Candidates with no readable source state, which fall back to a full
    #: authorization; logged so a regression in the batched read shows up.
    unobserved: set[str] = field(default_factory=set)
    unsendable: int = 0

    @classmethod
    async def load(cls, group_id: str, limit: int) -> _DreamSelection:
        reflected = await reflected_sources(group_id)
        paged = reflected.alone | await completed_dream_sources(group_id)
        return cls(group_id, reflected, paged, returning=limit // 2)

    def prior_pass(self, source: RawMemory, observation: tuple[str, int] | None) -> str | None:
        if observation is None:
            return None
        key = (source.principal_id or "", source.id, *observation)
        if key in self.reflected.cohort:
            return "covered"
        return "paged" if key in self.paged else None

    async def prefetch(self, memories: list[RawMemory]) -> None:
        missing = [
            memory.id
            for memory in memories
            if memory.id not in self.observed and memory.id not in self.unobserved
        ]
        if not missing:
            return
        found = await current_source_observations(self.group_id, missing)
        self.observed.update(found)
        self.unobserved.update(set(missing) - set(found))

    async def authorize(self, source: RawMemory) -> str:
        """Apply both gates to the source; name its pass at its current observation."""
        if source.id in self.authorized:
            return self.authorized[source.id]
        try:
            outcome = await self._authorize(source)
        except SourceUnavailableError:
            outcome = "unavailable"
        except Exception as exc:
            # Selected so the failure reaches the receipt, as before.
            self.errors[source.id] = exc
            outcome = "fresh"
        self.authorized[source.id] = outcome
        return outcome

    async def _authorize(self, source: RawMemory) -> str:
        work = await _load_dream_work(self.group_id, source)
        if work is None:
            return "unavailable"
        observation = work.snapshot.observation
        prior = self.prior_pass(source, (observation.effective_incarnation, observation.generation))
        if prior == "covered":
            return "covered"
        # The same check cohort preparation makes, on the snapshot just loaded.
        try:
            observe_raw_capture(work.snapshot.memory, await self._send_authority(source))
        except SourceUnavailableError:
            self.unsendable += 1
            raise
        self.selected[source.id] = work
        return prior or "fresh"

    async def _send_authority(self, source: RawMemory) -> SourceReadAuthority:
        from sibyl.jobs import ordinary_cohorts

        principal = source.principal_id or ""
        if principal not in self.send_authorities:
            try:
                authority = await ordinary_cohorts.writable_source_authority(
                    self.group_id, principal
                )
            except (SourceUnavailableError, InvalidAuthClaimsError, UserNotFoundError):
                # A principal the auth store no longer knows cannot send either.
                authority = None
            self.send_authorities[principal] = authority
        authority = self.send_authorities[principal]
        if authority is None:
            raise SourceUnavailableError
        return authority

    async def fresh(self, source: RawMemory) -> bool:
        if self.prior_pass(source, self.observed.get(source.id)) is not None:
            return False
        return await self.authorize(source) == "fresh"

    async def neighbour(self, source: RawMemory) -> bool:
        prior = self.prior_pass(source, self.observed.get(source.id))
        if prior == "covered" or (prior == "paged" and self.returning == 0):
            return False
        outcome = await self.authorize(source)
        if outcome not in {"fresh", "paged"}:
            return False
        # Either read can show the source was reflected alone; an error while
        # authorizing it must not let it past the limit as fresh.
        if "paged" in {prior, outcome}:
            if self.returning == 0:
                return False
            self.returning -= 1
        return True


async def _dream_page(
    group_id: str,
    walk: list[RawMemory],
    limit: int,
    is_neighbour: Callable[[RawMemory], Awaitable[bool]],
    prefetch: Callable[[list[RawMemory]], Awaitable[None]] | None = None,
) -> tuple[list[RawMemory], set[str]]:
    """Seed the page at the cursor, fill it with the seed's neighbours, then walk on.

    The walk holds only sources no pass has reflected at their current
    observation. Its first source seeds the page and the seed's nearest
    neighbours join it, so a family spread across identifier order meets in
    one partition instead of being cut at page boundaries. A source already
    reflected on its own may come back only here, beside a fresh seed, where
    a family member that arrived later can finally meet it.

    Walk sources fill what the neighbours leave. The page always holds a
    prefix of the walk: the seed, then walk sources in order until the page
    is full, with neighbours counted where they fall. Only that prefix
    advances the cursor, and the page lists it first in walk order, so the
    cursor never passes a source that did not get a place, and a crash leaves
    it on the last walk source handled. The owner's neighbour test admits at
    most half a page of sources reflected alone, so every page holds at least
    half a page of fresh sources whenever that many remain, however many
    reflected sources crowd the seed. A fresh neighbour taken from beyond the
    cursor is reflected early; once its cohort completes, the walk rules it
    out and moves past it. Returns the page and the identifiers of its walk
    prefix.
    """
    if not walk:
        return [], set()
    seed = walk[0]
    neighbours = await list_reflection_dream_neighbours(
        organization_id=group_id,
        seed=seed,
        limit=limit - 1,
        is_pending=is_neighbour,
        prefetch=prefetch,
    )
    chosen = {seed.id, *(source.id for source in neighbours)}
    prefix, room = [seed], limit - len(chosen)
    for source in walk[1:]:
        if source.id not in chosen:
            if room == 0:
                break
            room -= 1
        prefix.append(source)
    walked = {source.id for source in prefix}
    return [*prefix, *(source for source in neighbours if source.id not in walked)], walked


async def _load_dream_work(group_id: str, source: RawMemory) -> DreamSourceWork | None:
    if not source.principal_id or not source.raw_content.strip():
        return None
    readable = frozenset(await _accessible_projects_for_source(group_id=group_id, source=source))
    writable = frozenset(
        await _resolve_accessible_projects(
            group_id=group_id,
            principal_id=source.principal_id,
            required_role=ProjectRole.CONTRIBUTOR,
        )
    )
    snapshot = await load_authorized_source_snapshot(
        SourceIdentity(group_id, SourceKind.RAW_CAPTURE, source.id),
        SourceReadAuthority(source.principal_id, projects=readable),
        organization_id=group_id,
    )
    if not isinstance(snapshot, RawSourceSnapshot):
        raise SourceUnavailableError
    if snapshot.memory.principal_id != source.principal_id:
        raise SourceUnavailableError
    return DreamSourceWork(snapshot, readable, writable)


async def _reflect_dream_source(
    *,
    source: RawMemory,
    work: DreamSourceWork | None = None,
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

    if work is not None:
        source = work.snapshot.memory
    accessible_projects = await _accessible_projects_for_source(group_id=group_id, source=source)
    dream_kwargs = {}
    if work is not None and not dry_run:
        dream_kwargs = {"extractor": CheckpointReflectionExtractor(work), "dream_work": work}
    with llm_budget_context(user_id=source.principal_id, organization_id=group_id):
        pack = await reflect_memory(
            source.raw_content,
            **dream_kwargs,
            source_title=source.title or source.source_id or source.id,
            intent="maintenance",
            domain=_metadata_str(source.metadata, "domain"),
            project=source.project_id,
            related_to=_metadata_str_list(source.metadata.get("related_to")),
            organization_id=group_id,
            principal_id=source.principal_id,
            accessible_projects=accessible_projects,
            writable_projects=await _resolve_accessible_projects(
                group_id=group_id,
                principal_id=source.principal_id,
                required_role=ProjectRole.CONTRIBUTOR,
            ),
            memory_scope=source.memory_scope,
            scope_key=source.scope_key,
            suggested_memory_scope=_metadata_str(source.metadata, "suggested_memory_scope"),
            suggested_scope_key=_metadata_str(source.metadata, "suggested_scope_key"),
            persist=not dry_run,
            persist_source=False,
            persist_review=not dry_run,
            existing_source_id=source.id,
        )
    if work is None or dry_run:
        return await _mark_source_reflected(source, pack=pack, run_id=run_id, dry_run=dry_run)
    result = {
        "source_id": source.id,
        "outcome": "reflected",
        "candidate_count": len(pack.candidates),
        "persisted_count": pack.persisted_count,
        "operation_id": work.key,
        "candidate_ids": [candidate.persisted_id for candidate in pack.candidates],
    }
    current = await _load_dream_work(group_id, source)
    if current is None or current.key != work.key or not await complete_dream_stage(work, result):
        raise SourceUnavailableError
    return result


async def _drain_dream_candidates(
    *,
    group_id: str,
    run_id: str,
    dry_run: bool,
    limit: int,
    confidence_threshold: float | None,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    results: list[dict[str, Any]] = []
    handled_frontiers: set[str] = set()
    remaining = limit
    cursor = None
    while remaining > 0:
        # The walk does not read page length as an end signal. The reader filters
        # rows of its own after the query limit, so coupling the drain to how
        # many rows came back makes a reader change able to strand candidates
        # silently. Only an empty page ends the walk; the keyset cursor advances
        # per candidate, so the cost is one extra query and every pending
        # candidate is still visited exactly once.
        page_size = remaining
        candidates = await list_reflection_candidate_reviews(
            organization_id=group_id,
            review_state="pending",
            limit=page_size,
            after=cursor,
        )
        if not candidates:
            break
        for candidate in candidates:
            cursor = (candidate.captured_at or candidate.created_at, candidate.id)
            if candidate.id in handled_frontiers:
                continue
            if run_ceiling_reached():
                # The candidate stays pending for the next run; the walk ends
                # here rather than writing a skip for every pending candidate.
                results.append(
                    {"candidate_id": candidate.id, "outcome": "skip", "reason": RUN_TOKEN_CEILING}
                )
                return results
            try:
                with llm_budget_context(
                    user_id=candidate.principal_id or None, organization_id=group_id
                ):
                    result = await _drain_dream_candidate(
                        candidate=candidate,
                        group_id=group_id,
                        run_id=run_id,
                        dry_run=dry_run,
                        confidence_threshold=confidence_threshold,
                        handled_frontiers=handled_frontiers,
                    )
                results.append(result)
                if result["outcome"] != "skip":
                    remaining -= 1
            except Exception as exc:
                remaining -= 1
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
                        "provider_error": provider_error_detail(exc),
                        "dry_run": dry_run,
                        **budget_failure_fields(exc),
                    }
                )
    return results


async def _drain_dream_candidate(
    *,
    candidate: RawMemory,
    group_id: str,
    run_id: str,
    dry_run: bool,
    confidence_threshold: float | None,
    handled_frontiers: set[str] | None = None,
) -> dict[str, Any]:
    automatic_executions: list[str] = []
    validation_promotion = None
    if not dry_run:
        from sibyl.jobs.ordinary_cohorts import writable_source_authority
        from sibyl_core.services.automatic_reflection import automatically_review_reflection

        automatic = await automatically_review_reflection(
            group_id, str(candidate.principal_id or ""), candidate.id, writable_source_authority
        )
        automatic_executions = list(automatic.executions)
        if automatic.candidate is None:
            pending = automatic.status == "pending"
            return {
                "candidate_id": candidate.id,
                "outcome": "pending" if pending else "abstained",
                "recommended_action": "retain" if pending else "abstain",
                "applied": False,
                "archived": not pending,
                "dry_run": False,
                "reason": automatic.reason,
                "review_state": "pending" if pending else "archived",
                "promoted_id": None,
                "raw_source_ids": [],
                "policy_reasons": [],
                "exception_reasons": [],
                "confidence": None,
                "validation_executions": automatic_executions,
            }
        candidate = automatic.candidate
        if handled_frontiers is not None:
            handled_frontiers.update(automatic.candidate_ids or (candidate.id,))
        from sibyl_core.services.ordinary_publication import ordinary_promotion_binding

        async def authorize_publication():
            await writable_source_authority(group_id, str(candidate.principal_id or ""))

        validation_promotion = await ordinary_promotion_binding(
            group_id,
            str(candidate.principal_id or ""),
            candidate.id,
            automatic_executions[-1],
            writable_source_authority,
            authorize_publication,
        )
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
        writable_projects=await _resolve_accessible_projects(
            group_id=group_id,
            principal_id=candidate.principal_id,
            required_role=ProjectRole.CONTRIBUTOR,
        ),
    )
    if validation_promotion is not None:
        from sibyl_core.services.memory_autonomy import reflection_autonomy_candidate_metadata
        from sibyl_core.services.reflection_validation import prepare_stored_reflection

        current = await prepare_stored_reflection(
            group_id,
            str(candidate.principal_id or ""),
            candidate.id,
            writable_source_authority,
            publication=True,
        )
        flags = set((preview.metadata or {}).get("sensitivity_flags", []))
        for source in current.sources:
            flags.update(reflection_autonomy_candidate_metadata(source)["sensitivity_flags"])
        preview = replace(
            preview,
            reason="candidate_already_promoted"
            if current.memory.review_state == "promoted"
            else preview.reason,
            metadata={**(preview.metadata or {}), "sensitivity_flags": sorted(flags)},
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
        validated_source_support=validation_promotion is not None,
    )
    superseded_retired: list[str] = []
    if (
        not dry_run
        and decision.outcome is ReflectionAutonomyOutcome.SKIP
        and decision.reason == "candidate_already_promoted"
    ):
        # The chain frontier is already published, so the drafts it replaced are
        # terminal too. Databases written before promotion retired them heal here.
        from sibyl_core.services.reflection_supersession import (
            retire_superseded_reflection_drafts,
        )

        superseded_retired = await retire_superseded_reflection_drafts(
            organization_id=group_id, promoted_candidate_id=candidate.id
        )
        if handled_frontiers is not None:
            handled_frontiers.update(superseded_retired)

    promotion: ReflectionPromotionResult | None = None
    if decision.should_promote:
        promotion = await promote_reflection_candidate_review(
            validation_promotion=validation_promotion,
            candidate_id=candidate.id,
            expected_candidate_revision=candidate.revision,
            organization_id=group_id,
            principal_id=candidate.principal_id,
            promote_to_scope=target_scope,
            promote_to_scope_key=target_scope_key,
            domain=_metadata_str(candidate.metadata, "domain"),
            project=project,
            related_to=_metadata_str_list(candidate.metadata.get("related_to")),
            accessible_projects=accessible_projects,
            writable_projects=await _resolve_accessible_projects(
                group_id=group_id,
                principal_id=candidate.principal_id,
                required_role=ProjectRole.CONTRIBUTOR,
            ),
        )

    archived = False
    review_state = promotion.review_state if promotion else decision.review_state
    if decision.outcome is ReflectionAutonomyOutcome.EXCEPTION and not dry_run:
        archived_memory = await _archive_dream_exception_candidate(
            candidate=candidate,
            decision_reason=decision.reason,
            exception_reasons=decision.exception_reasons,
            run_id=run_id,
        )
        archived = True
        review_state = archived_memory.review_state

    outcome = "abstained" if archived else decision.outcome.value
    reason = decision.reason
    applied = promotion is not None and promotion.success
    promoted_id = promotion.promoted_id if promotion is not None and applied else None
    if promotion is not None and not promotion.success:
        outcome = "error"
        reason = promotion.reason

    await _log_dream_candidate_audit(
        candidate=candidate,
        group_id=group_id,
        run_id=run_id,
        dry_run=dry_run,
        preview_allowed=preview.allowed,
        decision_reason=reason,
        outcome=outcome,
        recommended_action="abstain" if archived else decision.recommended_action.value,
        memory_scope=decision.memory_scope.value if decision.memory_scope else target_scope,
        scope_key=decision.scope_key or target_scope_key,
        project=project,
        raw_source_ids=decision.raw_source_ids,
        promoted_id=promoted_id,
        exception_reasons=decision.exception_reasons,
        review_state=review_state,
    )
    return {
        "validation_executions": automatic_executions,
        "candidate_id": candidate.id,
        "outcome": outcome,
        "recommended_action": "abstain" if archived else decision.recommended_action.value,
        "applied": applied,
        "archived": archived,
        "dry_run": dry_run,
        "reason": reason,
        "review_state": review_state,
        "promoted_id": promoted_id,
        "superseded_retired": superseded_retired,
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
    await save_raw_memory(replace(source, metadata=metadata), expected_revision=source.revision)
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
    await save_raw_memory(replace(source, metadata=metadata), expected_revision=source.revision)
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
        "autonomy_recommended_action": "abstain",
        "reflection_dream_run_id": run_id,
    }
    return await save_raw_memory(
        replace(candidate, review_state="archived", metadata=metadata),
        expected_revision=candidate.revision,
    )


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
    required_role: ProjectRole = ProjectRole.VIEWER,
) -> set[str]:
    if not principal_id:
        return set()
    try:
        project_ids = await resolve_accessible_project_graph_ids(
            user_id=principal_id,
            org_id=group_id,
            required_role=required_role,
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
        if outcome == ReflectionAutonomyOutcome.AUTO_PROMOTE.value and not dry_run
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
            "superseded_retired",
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
