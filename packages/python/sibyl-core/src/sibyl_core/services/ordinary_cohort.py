"""Propose ordinary memories from one authorized, durable cohort observation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from threading import Event

from pydantic import TypeAdapter
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import Model

from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.config import settings
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.content_raw_persistence import (
    get_raw_memory,
    remember_reflection_candidate_review,
)
from sibyl_core.services.memory_source_validation import (
    SourceAuthorityResolver,
    SourceReadAuthority,
)
from sibyl_core.services.observed_sources import load_authorized_source_snapshot
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.services.source_state_store import RawSourceSnapshot
from sibyl_core.services.validation_candidate import ValidationCandidateWrite
from sibyl_core.services.validation_execution import ValidationExecution, _query
from sibyl_core.services.validation_stages import run_validation_stage
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.consolidation import ConsolidationInputBudgetExceeded
from sibyl_core.tasks.episode_evidence import is_controller_episode
from sibyl_core.tasks.ordinary_evidence import OrdinarySource
from sibyl_core.tasks.ordinary_packets import OrdinaryEvidencePacket, prepare_ordinary_packets
from sibyl_core.tasks.ordinary_projection import (
    VERSION as COMPLETE_PROJECTION,
)
from sibyl_core.tasks.ordinary_projection import ProjectionReuse, prepare_ordinary_projection
from sibyl_core.tasks.ordinary_proposal_result import OrdinaryProposalResult
from sibyl_core.tasks.ordinary_proposals import (
    VERSION,
    PartialCohort,
    PartialEpisode,
    PartialProposal,
    PreparedPartialProposal,
    partial_packet_prompt,
    prepare_partial_proposal,
)
from sibyl_core.tasks.procedure_review import review_digest

COHORT_SNAPSHOT = """
LET $captures=(SELECT * OMIT embedding, retrieval_count, citation_count, misled_count, last_recalled_at, last_used_at, metadata.embedding_metadata FROM raw_captures WHERE organization_id=$org AND uuid IN $source_ids ORDER BY uuid);
LET $states=(SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id=$org
    AND source_kind='raw_capture' AND source_id IN $source_ids ORDER BY source_id);
LET $snapshot={captures:$captures,states:$states};
LET $snapshot_digest=crypto::sha256(type::string($snapshot));
"""
COHORT_AFFINITY = (
    "SELECT uuid, embedding, metadata.embedding_metadata AS space FROM raw_captures "
    "WHERE organization_id=$org AND uuid IN $source_ids AND embedding != NONE ORDER BY uuid;"
)
COHORT_GUARD = (
    COHORT_SNAPSHOT
    + "IF $snapshot_digest!=$expected { THROW 'Ordinary cohort sources changed'; };"
    + "LET $source_states_to_fence=$states;"
    + SOURCE_STATE_WRITE_WITNESS
)


@dataclass(frozen=True)
class PreparedCohort:
    prepared: PreparedPartialProposal
    sources: tuple[RawSourceSnapshot, ...]
    snapshot_sha256: str
    authority: SourceReadAuthority

    @property
    def ids(self) -> list[str]:
        return [source.memory.id for source in self.sources]

    @property
    def bindings(self) -> list[dict[str, object]]:
        return [
            {
                "source_id": s.memory.id,
                "incarnation": s.observation.effective_incarnation,
                "generation": s.observation.generation,
            }
            for s in self.sources
        ]


async def prepare_stored_cohort(
    org: str,
    principal: str,
    source_ids: list[str],
    resolver: SourceAuthorityResolver,
    *,
    packet_binding: dict | None = None,
    allow_single: bool = False,
    evidence_mode: str = "auto",
    projection_binding: dict | None = None,
) -> PreparedCohort:
    """Resolve actual retained sources; project grouping never proves environment facts."""
    ids = sorted(source_ids)
    if len(ids) < (1 if allow_single or packet_binding else 2) or len(ids) != len(set(ids)):
        raise ValueError("An ordinary cohort requires distinct retained sources")
    authority = await resolver(org, principal)
    if authority is None or authority.principal_id != principal:
        raise SourceUnavailableError()
    rows = await _query(
        "RETURN {" + COHORT_SNAPSHOT + "RETURN {snapshot:$snapshot,token:$snapshot_digest}; };",
        org=org,
        source_ids=ids,
    )
    if len(rows) != 1:
        raise SourceUnavailableError()
    data = rows[0]["snapshot"]
    captures = {r["uuid"]: r for r in data["captures"]}
    states = {r["source_id"]: r for r in data["states"]}
    if set(captures) != set(ids) or set(states) != set(ids):
        raise SourceUnavailableError()
    sources = []
    episodes = []
    scope = None
    for identifier in ids:
        source = await load_authorized_source_snapshot(
            SourceIdentity(org, SourceKind.RAW_CAPTURE, identifier), authority, organization_id=org
        )
        if not isinstance(source, RawSourceSnapshot):
            raise SourceUnavailableError()
        memory = source.memory
        state = states[identifier]
        identity = (memory.memory_scope, memory.scope_key, memory.project_id)
        if memory.principal_id != principal or (scope is not None and identity != scope):
            raise SourceUnavailableError()
        scope = identity
        if (
            memory.raw_content != captures[identifier]["raw_content"]
            or memory.revision != captures[identifier]["revision"]
            or source.observation.effective_incarnation != state["incarnation"]
            or source.observation.generation != state["generation"]
        ):
            raise SourceUnavailableError()
        if memory.capture_surface in {"reflection_candidate", "reflection_source"} or (
            memory.entity_type in {"procedure", "pattern"} and memory.derivation_required
        ):
            raise SourceUnavailableError()
        artifact = memory.raw_content.encode("utf-8")
        episodes.append(
            PartialEpisode(
                episode_id=identifier,
                artifact=artifact,
                source=OrdinarySource(
                    source_id=identifier,
                    incarnation=source.observation.effective_incarnation,
                    generation=source.observation.generation,
                    observed_revision=memory.revision,
                    content_sha256=hashlib.sha256(artifact).hexdigest(),
                ),
            )
        )
        sources.append(source)
    assert scope is not None
    group = PartialCohort(
        group_id=review_digest(ids),
        mechanism="Compare ordinary retained observations",
        organization_id=org,
        owner_principal_id=principal,
        memory_scope=scope[0],
        scope_key=scope[1],
        episodes=tuple(episodes),
    )
    packet = None
    if packet_binding is not None:
        from sibyl_core.tasks.ordinary_packets import reconstruct_ordinary_packet

        if len(episodes) != 1:
            raise SourceUnavailableError()
        if packet_binding.get("manifest", {}).get("source_observation") != episodes[
            0
        ].source.model_dump(mode="json"):
            raise SourceUnavailableError()
        packet = await asyncio.to_thread(
            reconstruct_ordinary_packet, ids[0], episodes[0].artifact, packet_binding
        )
    return PreparedCohort(
        await asyncio.to_thread(
            _prepare_cohort_input,
            group,
            packet=packet,
            evidence_mode=evidence_mode,
            projection_binding=projection_binding,
        ),
        tuple(sources),
        rows[0]["token"],
        authority,
    )


def _prepare_cohort_input(
    group: PartialCohort,
    *,
    packet: OrdinaryEvidencePacket | None = None,
    evidence_mode: str = "auto",
    projection_binding: dict | None = None,
    projection_reuse: ProjectionReuse | None = None,
) -> PreparedPartialProposal:
    if evidence_mode not in {"auto", "raw_v1", COMPLETE_PROJECTION}:
        raise ValueError("unsupported ordinary evidence representation")
    if packet is not None:
        if projection_binding is not None or evidence_mode == COMPLETE_PROJECTION:
            raise ValueError("ordinary evidence must select one representation")
        return prepare_partial_proposal(group, packet=packet)
    complete = evidence_mode == COMPLETE_PROJECTION or (
        evidence_mode == "auto" and all(is_controller_episode(e.artifact) for e in group.episodes)
    )
    if projection_binding is not None and not complete:
        raise ValueError("ordinary projection binding requires complete representation")
    projection = None
    if complete:
        if not all(isinstance(e, PartialEpisode) for e in group.episodes):
            raise ValueError("ordinary projection requires retained ordinary sources")
        projection = prepare_ordinary_projection(
            [(e.episode_id, e.artifact) for e in group.episodes],
            [e.source for e in group.episodes if isinstance(e, PartialEpisode)],
            reuse=projection_reuse,
        )
        if projection_binding is not None and projection.binding_json != canonical(
            projection_binding
        ):
            raise ValueError("ordinary projection differs from protected execution")
    return prepare_partial_proposal(group, projection=projection, projection_reuse=projection_reuse)


def _cohort_input_chars(
    prepared: PreparedPartialProposal,
    proposal_schema_chars: int,
    critic_schema_chars: int,
    *,
    projection_reuse: ProjectionReuse | None = None,
) -> int:
    """Fit both stages; candidate headroom never replaces the actual critic guard."""
    actual = len(prepared.system) + len(prepared.prompt) + proposal_schema_chars
    if prepared.projection_json:
        from sibyl_core.tasks.memory_validation import projection_critic_input_chars
        from sibyl_core.tasks.ordinary_proposals import _projection_for_cohort

        projection = _projection_for_cohort(
            PartialCohort.model_validate_json(prepared.input_json),
            prepared.projection_json,
            reuse=projection_reuse,
        )
        assert projection is not None
        actual = max(
            actual,
            projection_critic_input_chars(
                projection, candidate_reserve_chars=settings.consolidation_max_input_chars // 4
            )
            + critic_schema_chars,
        )
    return actual


async def propose_stored_cohort(
    org: str,
    principal: str,
    source_ids: list[str],
    resolver: SourceAuthorityResolver,
    *,
    authorize: Callable[[], Awaitable[None]],
    packet_binding: dict | None = None,
    evidence_mode: str = "auto",
) -> tuple[RawMemory | None, str]:
    """Reuse completed results without redispatch, then persist their protected derivation."""
    from sibyl_core.services.procedure_validation import (
        _close_resources,
        _OwnedValidationExtractor,
        validation_extractor,
    )

    await authorize()
    original = await prepare_stored_cohort(
        org,
        principal,
        source_ids,
        resolver,
        packet_binding=packet_binding,
        evidence_mode=evidence_mode,
    )
    if len(original.prepared.prompt) > settings.consolidation_max_input_chars:
        raise ConsolidationInputBudgetExceeded(
            len(original.prepared.prompt), settings.consolidation_max_input_chars
        )
    owned, policy = await validation_extractor()
    try:
        extractor = await _proposal_extractor(owned, original.prepared.system)
        schema = await extractor.output_schema()
        projection_policy = (
            {
                "evidence_representation": COMPLETE_PROJECTION,
                "packing_policy": {
                    "version": "ordinary_complete_dual_envelope_v1",
                    "max_input_chars": settings.consolidation_max_input_chars,
                    "candidate_reserve_chars": settings.consolidation_max_input_chars // 4,
                },
            }
            if original.prepared.projection_json
            else {}
        )
        policy = canonical(
            {**json.loads(policy), "version": VERSION, "schema": schema, **projection_policy}
        )
        actual = _cohort_input_chars(
            original.prepared,
            len(canonical(schema)),
            len(canonical(await owned.output_schema())) if original.prepared.projection_json else 0,
        )
        if actual > settings.consolidation_max_input_chars:
            raise ConsolidationInputBudgetExceeded(actual, settings.consolidation_max_input_chars)
        return await _run_cohort(org, principal, original, resolver, extractor, policy, authorize)
    finally:
        if isinstance(owned, _OwnedValidationExtractor):
            await _close_resources(owned.resources)


async def _run_cohort(org, principal, original, resolver, extractor, policy, authorize):
    async def current():
        await authorize()
        refreshed = await prepare_stored_cohort(
            org,
            principal,
            original.ids,
            resolver,
            packet_binding=json.loads(original.prepared.packet_json)
            if original.prepared.packet_json
            else None,
            evidence_mode=COMPLETE_PROJECTION if original.prepared.projection_json else "raw_v1",
            projection_binding=json.loads(original.prepared.projection_json)
            if original.prepared.projection_json
            else None,
        )
        if (
            refreshed.snapshot_sha256 != original.snapshot_sha256
            or refreshed.prepared != original.prepared
        ):
            raise SourceUnavailableError()

    request = {
        "kind": VERSION,
        "org": org,
        "principal": principal,
        "parent": original.ids[0],
        "source_bindings": original.bindings,
        "snapshot": original.snapshot_sha256,
        "input": original.prepared.input_sha256,
        # The input digest covers the evidence only, so the instructions bind here:
        # reworded instructions must not replay a result produced under the old ones.
        "prompt": original.prepared.prompt_sha256,
        "policy": policy,
        **(
            {"evidence_projection": json.loads(original.prepared.projection_json)}
            if original.prepared.projection_json
            else {}
        ),
        **(
            {"evidence_packet": json.loads(original.prepared.packet_json)}
            if original.prepared.packet_json
            else {}
        ),
    }
    identity = review_digest(request)
    params = {
        "org": org,
        "principal": principal,
        "source_ids": original.ids,
        "expected": original.snapshot_sha256,
    }
    execution = ValidationExecution(
        identity,
        org,
        principal,
        authorize=current,
        dispatch_guard=COHORT_GUARD,
        guard_params={k: v for k, v in params.items() if k not in {"org", "principal"}},
    )

    async def run():
        result = await extractor.extract_with_usage(original.prepared.prompt)
        proposal = PartialProposal.model_validate(result.output.model_dump())
        error = None
        try:
            original.prepared.render(proposal)
        except ValueError as failure:
            error = str(failure)
        return OrdinaryProposalResult(
            "ordinary_cohort_proposal",
            original.prepared.input_sha256,
            proposal,
            result.usage,
            error,
        )

    try:
        value = await run_validation_stage(
            execution=execution,
            parent_id=original.ids[0],
            source_ids=original.ids,
            request=request,
            policy=policy,
            check_current=current,
            run=run,
        )
    except Exception as error:
        error.__dict__["execution_id"] = identity
        try:
            failed_stage = await execution.load()
        except Exception as lookup_error:
            error.__dict__["execution_lookup_error"] = type(lookup_error).__name__
        else:
            error.__dict__["execution_state"] = failed_stage.get("state") if failed_stage else None
        raise
    try:
        result = TypeAdapter(OrdinaryProposalResult).validate_python(
            {k: v for k, v in value.items() if k != "execution_id"}
        )
        if result.input_sha256 != original.prepared.input_sha256:
            raise SourceUnavailableError()
        if result.validation_error is not None:
            failure = ValueError(result.validation_error)
            failure.__dict__["extraction_usage"] = result.usage.model_dump(mode="json")
            failure.__dict__["execution_id"] = identity
            raise failure
        candidate = original.prepared.render(result.proposal)
        if candidate is None:
            return None, identity
        await current()
        row = await execution.load()
        if row is None:
            raise SourceUnavailableError()
        write = ValidationCandidateWrite(identity, row["result_json"], COHORT_GUARD, params)
        replay = await get_raw_memory(organization_id=org, memory_id=write.id)
        embedding = {"embedding_provider": None} if replay is not None else {}
        memory = await remember_reflection_candidate_review(
            **embedding,
            organization_id=org,
            principal_id=principal,
            candidate=candidate,
            raw_source_ids=original.ids,
            source_id=original.ids[0],
            memory_scope=original.sources[0].memory.memory_scope,
            scope_key=original.sources[0].memory.scope_key,
            suggested_memory_scope=candidate.suggested_memory_scope,
            suggested_scope_key=candidate.suggested_scope_key,
            source_observations=[source.observation for source in original.sources],
            validation_write=write,
            accessible_projects=original.authority.projects,
            accessible_teams=original.authority.teams,
            accessible_delegations=original.authority.delegations,
            allowed_memory_scope_keys=original.authority.scope_keys,
        )
        return memory, identity
    except Exception as error:
        error.__dict__["execution_id"] = identity
        error.__dict__["execution_state"] = "returned"
        raise


async def _proposal_extractor(owned, system: str):
    model = (await owned._get_agent()).model
    if not isinstance(model, Model):
        raise ValueError("Cohort proposal needs a resolved model")
    extractor = Extractor(
        PartialProposal,
        agent=Agent(
            model,
            system_prompt=system,
            output_type=NativeOutput(PartialProposal, strict=True)
            if owned.output_mode == "native_strict"
            else PartialProposal,
            retries={"output": 2},
        ),
        surface=owned.surface,
        system_prompt=system,
        max_tokens=owned.max_tokens,
        output_retries=2,
        output_mode=owned.output_mode,
        openrouter_provider=owned.openrouter_provider,
        model_override=owned.model_override,
    )
    return extractor


async def partition_stored_cohort(org, principal, source_ids, resolver):
    """Pack complete authorized episodes against the actual resolved schema budget."""
    from sibyl_core.services.procedure_validation import (
        _close_resources,
        _OwnedValidationExtractor,
        validation_extractor,
    )

    original = await prepare_stored_cohort(org, principal, source_ids, resolver)
    affinity = await _cohort_affinity(org, original.ids)
    owned, _policy = await validation_extractor()
    try:
        extractor = await _proposal_extractor(owned, original.prepared.system)
        schema_chars = len(canonical(await extractor.output_schema()))
        # Any cohort of controller episodes is sized with the critic's envelope,
        # even when the whole group mixes representations.
        critic_schema_chars = len(canonical(await owned.output_schema()))
    finally:
        if isinstance(owned, _OwnedValidationExtractor):
            await _close_resources(owned.resources)
    cancelled = Event()
    try:
        return await asyncio.to_thread(
            _partition_prepared_cohort,
            original.prepared,
            schema_chars,
            critic_schema_chars,
            cancelled,
            affinity,
        )
    finally:
        cancelled.set()


async def _cohort_affinity(org: str, source_ids: list[str]) -> dict[str, tuple[float, ...]]:
    """Unit vectors from the embedding space most sources share.

    Vectors from different models or text versions are not comparable, so every
    source outside the largest space, and every unembedded one, keeps budget-only
    packing.
    """
    spaces: dict[str, dict[str, tuple[float, ...]]] = {}
    for row in await _query(COHORT_AFFINITY, org=org, source_ids=sorted(source_ids)):
        vector = row.get("embedding") or ()
        norm = math.sqrt(math.fsum(value * value for value in vector))
        if norm:
            spaces.setdefault(canonical(row.get("space")), {})[row["uuid"]] = tuple(
                value / norm for value in vector
            )
    if not spaces:
        return {}
    return min(spaces.items(), key=lambda item: (-len(item[1]), item[0]))[1]


def _partition_prepared_cohort(
    original: PreparedPartialProposal,
    schema_chars: int,
    critic_schema_chars: int,
    cancelled: Event,
    affinity: Mapping[str, tuple[float, ...]] | None = None,
) -> list[list[str]]:
    """Keep pure evidence preparation off the event loop and stop cancelled work.

    Embedded episodes grow each cohort from its seed's nearest neighbours, so a
    proposal compares related experience instead of whatever shared a page of
    identifiers. Two grown cohorts that together rank themselves first join
    when the union fits. Every other episode then joins the most similar cohort
    that still fits, or the first one when it has no comparable vector, so a
    cohort of one happens only when the budget forces it.
    """
    group = PartialCohort.model_validate_json(original.input_json)
    cohort_fields = group.model_dump(exclude={"episodes"})
    projection_reuse = ProjectionReuse()

    def fits(episodes):
        if cancelled.is_set():
            raise asyncio.CancelledError
        if len(episodes) < 2:
            return True
        ids = [e.episode_id for e in episodes]
        partial = PartialCohort.model_validate(
            {**cohort_fields, "episodes": tuple(episodes), "group_id": review_digest(ids)}
        )
        # Size each cohort in the representation its proposal will use: a group
        # that mixes controller and plain captures can still yield a cohort of
        # controller episodes, which goes out as a projection with a critic reserve.
        prepared = _prepare_cohort_input(
            partial, evidence_mode="auto", projection_reuse=projection_reuse
        )
        return (
            _cohort_input_chars(
                prepared, schema_chars, critic_schema_chars, projection_reuse=projection_reuse
            )
            <= settings.consolidation_max_input_chars
        )

    vectors = affinity or {}
    related = [episode for episode in group.episodes if episode.episode_id in vectors]
    similarity = _similarity(related, vectors) if len(related) >= 2 else {}
    bins, leftovers = _nearest_neighbour_bins(related, similarity, fits) if similarity else ([], [])
    bins = _merge_separated_bins(bins, similarity, fits)
    leftovers = sorted(
        [
            *leftovers,
            *(episode for episode in group.episodes if episode.episode_id not in similarity),
        ],
        key=lambda episode: episode.episode_id,
    )
    for episode in leftovers:
        if cancelled.is_set():
            raise asyncio.CancelledError
        scores = similarity.get(episode.episode_id, {})

        def affinity_to(bucket, scores=scores):
            known = [scores[member.episode_id] for member in bucket if member.episode_id in scores]
            return math.fsum(known) / len(known) if known else -math.inf

        # sorted() is stable, so an episode with no comparable vector keeps
        # plain first-fit order across the cohorts.
        for bucket in sorted(bins, key=affinity_to, reverse=True):
            if fits([*bucket, episode]):
                bucket.append(episode)
                break
        else:
            bins.append([episode])
    return [[episode.episode_id for episode in bucket] for bucket in bins]


#: An episode joins a cohort only when a member is among its own nearest
#: neighbours. A rank rather than a similarity cutoff, so it means the same
#: thing across embedding models and between boilerplate-heavy transcripts and
#: short notes. On the screen48 captures, paged and budgeted the way the dream
#: job runs, two neighbours keep 92 percent of a cohort in one task family with
#: no cohort of one, where nearest-neighbour growth alone keeps 75 and
#: identifier order keeps 24.
COHORT_NEIGHBOURS = 2


def _similarity(episodes, vectors) -> dict[str, dict[str, float]]:
    """Pairwise cosine similarity of unit vectors, computed once per partition."""
    ids = sorted(episode.episode_id for episode in episodes)
    similarity: dict[str, dict[str, float]] = {identifier: {} for identifier in ids}
    for index, left in enumerate(ids):
        for right in ids[index + 1 :]:
            value = math.fsum(a * b for a, b in zip(vectors[left], vectors[right], strict=True))
            similarity[left][right] = similarity[right][left] = value
    return similarity


def _nearest_neighbour_bins(episodes, similarity, fits):
    """Grow cohorts from nearest neighbours; return them with the episodes left over.

    Each bin starts from the first unplaced episode. It repeatedly takes the
    unplaced episode closest to its centroid among those that count a member
    among their own COHORT_NEIGHBOURS nearest unplaced neighbours, and skips one
    that does not fit, so a single oversized episode cannot close the bin for
    its whole family. Neighbours are ranked among unplaced episodes only, so a
    family whose first cohort filled still recognises its remaining members.
    Ties break on identifier, so the result depends only on the episodes, their
    vectors and the budget. A seed nothing reciprocates is returned as leftover.
    """
    remaining = sorted(episodes, key=lambda episode: episode.episode_id)
    bins, leftovers = [], []
    while remaining:
        seed = remaining.pop(0)
        pool = [seed.episode_id, *(episode.episode_id for episode in remaining)]
        neighbours = {
            identifier: set(
                sorted(
                    (other for other in pool if other != identifier),
                    key=lambda other, identifier=identifier: (
                        -similarity[identifier][other],
                        other,
                    ),
                )[:COHORT_NEIGHBOURS]
            )
            for identifier in pool
        }
        bucket, members, skipped = [seed], {seed.episode_id}, set()
        while candidates := [
            episode
            for episode in remaining
            if episode.episode_id not in skipped and neighbours[episode.episode_id] & members
        ]:
            # A centroid of unit vectors scores a candidate by the sum of its
            # similarities to the members. candidates stay sorted and max() keeps
            # the first of equal keys, so a tie goes to the smaller identifier.
            nearest = max(
                candidates,
                key=lambda episode: math.fsum(
                    similarity[member][episode.episode_id] for member in members
                ),
            )
            if fits([*bucket, nearest]):
                bucket.append(nearest)
                members.add(nearest.episode_id)
                remaining.remove(nearest)
            else:
                skipped.add(nearest.episode_id)
        if len(bucket) > 1:
            bins.append(bucket)
        else:
            leftovers.append(seed)
    return bins, leftovers


#: How much nearer than the closest outsider a joined cohort's farthest member
#: must stay, for every member. Rank alone lets two families join once each is
#: the other's nearest and the budget has room: on the screen48 captures at a
#: 1.6M-character budget, two such pairs cleared the rank test with the
#: farthest sibling as distant as the nearest outsider, while every same-family
#: pair kept it under an eighth of that distance.
COHORT_MERGE_MARGIN = 2


def _merge_separated_bins(bins, similarity, fits):
    """Join pairs of grown cohorts that sit well apart from everything else.

    Growth only follows an episode's own nearest neighbours into a cohort, so a
    family whose nearest-neighbour graph has two islands closes as two cohorts
    at any budget, and each proposes a near duplicate of the other. A pair
    joins when every episode of the union ranks each of its union siblings
    ahead of every other ranked episode, its farthest sibling less than
    1/COHORT_MERGE_MARGIN as far away as its nearest outsider. That requires an
    outsider: a partition holding nothing but the pair has no contrast to tell
    one family's two modes from two families. The margin compares distances in
    the same neighbourhood rather than setting a similarity cutoff, and it is
    strict, since mixing families costs more than a split. A joined cohort never
    joins again in the same partition, so one merge cannot open the next and
    chain families together, and the budget can only veto a union: spare room
    is never evidence of affinity.
    """
    ranked = set(similarity)

    def sits_inside(identifier, union):
        scores = similarity[identifier]
        farthest_sibling = 1 - min(scores[other] for other in union if other != identifier)
        nearest_outsider = 1 - max(score for other, score in scores.items() if other not in union)
        return COHORT_MERGE_MARGIN * farthest_sibling < nearest_outsider

    merged, joined = list(bins), set()
    for first in range(len(bins)):
        for second in range(first + 1, len(bins)):
            if first in joined or second in joined:
                continue
            pair = (*bins[first], *bins[second])
            union = {episode.episode_id for episode in pair}
            if not ranked - union or not all(sits_inside(i, union) for i in union):
                continue
            episodes = sorted(pair, key=lambda episode: episode.episode_id)
            if fits(episodes):
                merged[first], merged[second] = episodes, None
                joined.update((first, second))
    return [bucket for bucket in merged if bucket is not None]


async def prepare_stored_source_packets(org, principal, source_id, resolver):
    """Fit complete exchanges against proposer and critic envelopes, without a send."""
    from sibyl_core.services.procedure_validation import (
        _close_resources,
        _OwnedValidationExtractor,
        validation_extractor,
    )
    from sibyl_core.tasks.memory_validation import packet_critic_input_chars

    original = await prepare_stored_cohort(org, principal, [source_id], resolver, allow_single=True)
    group = PartialCohort.model_validate_json(original.prepared.input_json)
    owned, _policy = await validation_extractor()
    try:
        proposer = await _proposal_extractor(owned, original.prepared.system)
        proposal_schema = canonical(await proposer.output_schema())
        critic_schema = canonical(await owned.output_schema())
    finally:
        if isinstance(owned, _OwnedValidationExtractor):
            await _close_resources(owned.resources)

    # Reserve a quarter of the total critic input for the rendered candidate,
    # including both occurrences. Unbounded output still needs the actual guard.
    candidate_reserve = settings.consolidation_max_input_chars // 4

    def input_chars(packet: OrdinaryEvidencePacket) -> int:
        return max(
            len(partial_packet_prompt(group, packet))
            + len(original.prepared.system)
            + len(proposal_schema),
            packet_critic_input_chars(packet, candidate_reserve_chars=candidate_reserve)
            + len(critic_schema),
        )

    episode = group.episodes[0]
    if not isinstance(episode, PartialEpisode):
        raise SourceUnavailableError()
    return await asyncio.to_thread(
        prepare_ordinary_packets,
        source_id,
        group.episodes[0].artifact,
        input_chars=input_chars,
        max_input_chars=settings.consolidation_max_input_chars,
        packing_policy={
            "version": "actual_envelopes_with_candidate_headroom_v1",
            "max_input_chars": settings.consolidation_max_input_chars,
            "critic_candidate_reserve_chars": candidate_reserve,
            "proposal_schema_sha256": hashlib.sha256(proposal_schema.encode()).hexdigest(),
            "critic_schema_sha256": hashlib.sha256(critic_schema.encode()).hexdigest(),
        },
        source_observation=episode.source.model_dump(mode="json"),
    )
