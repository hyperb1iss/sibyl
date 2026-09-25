"""Durable ordinary proposals use real retained sources and existing stage owners."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from threading import Event
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM, bootstrap_content_schema
from sibyl_core.services import ordinary_cohort as service
from sibyl_core.services import procedure_validation
from sibyl_core.services.content_raw_persistence import remember_raw_memory, save_raw_memory
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.reflection_validation import (
    prepare_stored_reflection,
    validate_reflection_stage,
)
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.tasks.memory_validation import CriticOutput
from tests.test_eval_publication_promotion import runtime as runtime
from tests.validation_policy import memory_model_factory, offline_policy


@pytest.fixture
async def content_store(monkeypatch):
    client = SurrealContentClient(url="memory://")
    try:
        await bootstrap_content_schema(client, reset=True)

        @asynccontextmanager
        async def session():
            yield client

        monkeypatch.setattr("sibyl_core.services.content_client.surreal_content_client", session)
        yield client
    finally:
        await client.close()


@pytest.fixture
async def cohort_sources(content_store, monkeypatch):
    from sibyl_core.services import content_models

    monkeypatch.setattr(content_models, "configured_raw_memory_embedding_provider", lambda: None)
    return [
        await remember_raw_memory(
            organization_id="org",
            principal_id="owner",
            source_id=f"source-{i}",
            raw_content=f"Capture {i}: inspect logs before changing configuration.",
            embedding_provider=None,
        )
        for i in range(2)
    ]


def install_proposal(monkeypatch, sources):
    assertion = {
        "statement": "Inspect logs before changing configuration",
        "label": "inferred",
        "support": [
            {
                "episode_id": sources[0].id,
                "start_byte": 0,
                "end_byte": len(sources[0].raw_content.encode()),
            }
        ],
    }
    output = {"procedure": {"kind": "pattern", "goal": assertion}, "abstention_reason": None}
    owned = Extractor(
        CriticOutput, agent=Agent(TestModel(custom_output_args=output), output_type=CriticOutput)
    )
    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(side_effect=lambda *_: (owned, offline_policy())),
    )
    return output


async def test_ordinary_cohort_actual_stage_replay_and_critic(
    cohort_sources, monkeypatch, content_store, runtime
):
    sources = cohort_sources
    install_proposal(monkeypatch, sources)
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    authorize = AsyncMock()
    args = ("org", "owner", [s.id for s in sources], resolver)
    first, execution = await service.propose_stored_cohort(*args, authorize=authorize)
    second, replayed = await service.propose_stored_cohort(*args, authorize=authorize)
    assert first and second and first.id == second.id and execution == replayed
    stages = await content_store.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 1 and stages[0]["state"] == "returned"
    assert json.loads(stages[0]["result_json"])["usage"]["requests"] == 1
    assert json.loads(stages[0]["request_json"])["kind"] == "sibyl-ordinary-partial-proposal-v1"
    assert stages[0]["parent_id"] in [s.id for s in sources]
    parent = await prepare_stored_reflection("org", "owner", first.id, resolver)
    assert {s.id for s in parent.sources} == {s.id for s in sources}
    critic = Extractor(
        CriticOutput,
        agent=Agent(TestModel(custom_output_args={"findings": []}), output_type=CriticOutput),
    )
    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(side_effect=lambda *_: (critic, offline_policy())),
    )
    result = await validate_reflection_stage(parent, resolver)
    assert result["status"] == "no_findings"
    stage = await service.ValidationExecution(result["execution_id"], "org", "owner").load()
    request = json.loads(stage["request_json"])
    assert request["kind"] == "ordinary_reflection_validation-v2"
    assert request["input"] == result["input_sha256"]
    from sibyl_core.services.ordinary_publication import ordinary_promotion_binding

    promotion = await ordinary_promotion_binding(
        "org", "owner", first.id, result["execution_id"], resolver, authorize
    )
    guard, params = await promotion.current_guard()
    assert (
        "publication_source_observation_changed" in guard
        and params["ordinary_source_ids"] == parent.source_ids
    )
    from sibyl_core.services.memory_reflection import promote_reflection_candidate_review

    promoted = await promote_reflection_candidate_review(
        organization_id="org",
        principal_id="owner",
        candidate_id=first.id,
        promote_to_scope="private",
        promote_to_scope_key="owner",
        validation_promotion=promotion,
    )
    assert promoted.success
    from sibyl_core.services.validation_promotion import validated_graph_current

    assert await validated_graph_current("org", promoted.promoted_id)
    replay = await promote_reflection_candidate_review(
        organization_id="org",
        principal_id="owner",
        candidate_id=first.id,
        promote_to_scope="private",
        promote_to_scope_key="owner",
        validation_promotion=promotion,
    )
    assert replay.success and replay.promoted_id == promoted.promoted_id


async def test_ordinary_cohort_source_change_fences_returned_usage(
    cohort_sources, monkeypatch, content_store
):
    sources = cohort_sources
    install_proposal(monkeypatch, sources)
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    authorize = AsyncMock()
    extract = Extractor.extract_with_usage

    async def change(self, prompt):
        result = await extract(self, prompt)
        await save_raw_memory(
            replace(sources[0], raw_content="new source body"),
            expected_revision=sources[0].revision,
            embedding_provider=None,
        )
        return result

    monkeypatch.setattr(Extractor, "extract_with_usage", change)
    with pytest.raises(SourceUnavailableError):
        await service.propose_stored_cohort(
            "org", "owner", [s.id for s in sources], resolver, authorize=authorize
        )
    stages = await content_store.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 1 and stages[0]["state"] == "fenced"
    assert json.loads(stages[0]["usage_json"])["requests"] == 1
    assert not await content_store.execute_query(
        "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate';"
    )


async def test_ordinary_cohort_foreign_authority_no_stage(
    cohort_sources, monkeypatch, content_store
):
    install_proposal(monkeypatch, cohort_sources)
    with pytest.raises(SourceUnavailableError):
        await service.propose_stored_cohort(
            "org",
            "owner",
            [s.id for s in cohort_sources],
            AsyncMock(return_value=None),
            authorize=AsyncMock(),
        )
    assert not await content_store.execute_query("SELECT * FROM memory_validation_executions;")


async def test_ordinary_cohort_metadata_cannot_grant_confidence_policy():
    from types import SimpleNamespace

    from sibyl_core.services.content_models import MemoryScope
    from sibyl_core.services.memory_autonomy import decide_reflection_candidate_autonomy

    preview = SimpleNamespace(
        allowed=True,
        candidate_id="candidate",
        reason="allowed",
        review_state="pending",
        memory_scope=MemoryScope.PRIVATE,
        scope_key="owner",
        raw_source_ids=["source"],
        metadata={
            "confidence": 0,
            "autonomy_confidence_basis": "validated_source_support",
            "validated_source_support": True,
        },
    )
    assert not decide_reflection_candidate_autonomy(preview).should_promote
    assert decide_reflection_candidate_autonomy(
        preview, validated_source_support=True
    ).should_promote
    preview.metadata["sensitivity_flags"] = ["secret"]
    assert not decide_reflection_candidate_autonomy(
        preview, validated_source_support=True
    ).should_promote


@pytest.mark.parametrize("raced", [False, True])
async def test_ordinary_cohort_native_candidate_source_race(
    cohort_sources, content_store, monkeypatch, raced
):
    import asyncio
    import os

    if not os.environ.get("SIBYL_COHORT_NATIVE_URL"):
        pytest.skip("native multi-connection transaction control")
    install_proposal(monkeypatch, cohort_sources)
    execute = content_store.execute_query
    mutated = False

    async def delayed(query, **kwargs):
        nonlocal mutated
        if "Correction stage changed" not in query or mutated:
            return await execute(query, **kwargs)
        mutated = True
        pending = asyncio.create_task(
            execute(query.replace("LET $stage=", "SLEEP 1s; LET $stage=", 1), **kwargs)
        )
        try:
            if raced:
                await asyncio.sleep(0.25)
                changed = await execute(
                    "UPDATE raw_captures SET raw_content='concurrent committed source',revision+=1 WHERE uuid=$source RETURN AFTER;",
                    source=cohort_sources[0].id,
                )
                assert len(changed) == 1
            return await pending
        finally:
            if not pending.done():
                await pending

    monkeypatch.setattr(content_store, "execute_query", delayed)

    async def run():
        return await service.propose_stored_cohort(
            "org",
            "owner",
            [s.id for s in cohort_sources],
            AsyncMock(return_value=SourceReadAuthority("owner")),
            authorize=AsyncMock(),
        )

    if raced:
        with pytest.raises(Exception, match="cohort sources changed"):
            await run()
        assert not await execute(
            "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate';"
        )
        assert not await execute("SELECT * FROM memory_derivations;")
        rows = await execute(
            "SELECT raw_content FROM raw_captures WHERE uuid=$source;", source=cohort_sources[0].id
        )
        assert rows[0]["raw_content"] == "concurrent committed source"
    else:
        candidate, _ = await run()
        assert candidate is not None
    stages = await execute("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 1 and stages[0]["state"] == "returned"
    assert json.loads(stages[0]["usage_json"])["requests"] == 1


async def test_ordinary_cohort_completed_receipt_recovers_without_provider(
    cohort_sources, monkeypatch, content_store
):
    from sibyl_core.services.validation_execution import ValidationExecution

    install_proposal(monkeypatch, cohort_sources)
    original_finish = ValidationExecution._finish
    original_reconcile = ValidationExecution.reconcile_result
    monkeypatch.setattr(
        ValidationExecution, "_finish", AsyncMock(side_effect=OSError("database unavailable"))
    )
    monkeypatch.setattr(
        ValidationExecution,
        "reconcile_result",
        AsyncMock(side_effect=OSError("database unavailable")),
    )
    args = (
        "org",
        "owner",
        [s.id for s in cohort_sources],
        AsyncMock(return_value=SourceReadAuthority("owner")),
    )
    with pytest.raises(OSError, match="database unavailable"):
        await service.propose_stored_cohort(*args, authorize=AsyncMock())
    monkeypatch.setattr(ValidationExecution, "_finish", original_finish)
    monkeypatch.setattr(ValidationExecution, "reconcile_result", original_reconcile)
    monkeypatch.setattr(
        Extractor, "extract_with_usage", AsyncMock(side_effect=AssertionError("redispatch"))
    )
    candidate, _ = await service.propose_stored_cohort(*args, authorize=AsyncMock())
    assert candidate is not None
    stages = await content_store.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 1 and stages[0]["state"] == "returned"
    assert json.loads(stages[0]["usage_json"])["requests"] == 1


async def test_ordinary_cohort_reworded_instructions_do_not_replay_the_old_result(
    cohort_sources, monkeypatch, content_store
):
    from sibyl_core.tasks import ordinary_proposals

    install_proposal(monkeypatch, cohort_sources)
    args = (
        "org",
        "owner",
        [s.id for s in cohort_sources],
        AsyncMock(return_value=SourceReadAuthority("owner")),
    )
    _, first = await service.propose_stored_cohort(*args, authorize=AsyncMock())
    # Same sources and output schema; only the instructions change.
    monkeypatch.setattr(
        ordinary_proposals, "REQUEST", ordinary_proposals.REQUEST + " Prefer the shortest rule."
    )
    _, second = await service.propose_stored_cohort(*args, authorize=AsyncMock())
    assert first != second
    stages = await content_store.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 2


async def test_ordinary_cohort_budget_partition_keeps_all_source_bytes(
    cohort_sources, content_store, monkeypatch
):
    from sibyl_core.config import settings
    from sibyl_core.tasks._evidence_json import canonical
    from sibyl_core.tasks.ordinary_proposals import PartialProposal

    sources = [
        await remember_raw_memory(
            organization_id="org",
            principal_id="owner",
            source_id=f"large-{i}",
            raw_content=f"capture-{i}:" + "a" * 3000,
            embedding_provider=None,
        )
        for i in range(20)
    ]
    install_proposal(monkeypatch, sources)
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    monkeypatch.setattr(settings, "consolidation_max_input_chars", 40000)
    ids = [s.id for s in sources]
    bins = await service.partition_stored_cohort("org", "owner", ids, resolver)
    assert len(bins) > 1
    assert sorted(i for bucket in bins for i in bucket) == sorted(ids)
    assert bins == await service.partition_stored_cohort(
        "org", "owner", list(reversed(ids)), resolver
    )
    for bucket in bins:
        if len(bucket) < 2:
            continue
        prepared = await service.prepare_stored_cohort("org", "owner", bucket, resolver)
        from sibyl_core.ai.llm.extractor import extraction_schema

        assert (
            len(prepared.prepared.system)
            + len(prepared.prepared.prompt)
            + len(canonical(extraction_schema(PartialProposal)))
            <= 40000
        )
        for source in sources:
            if source.id in bucket:
                assert source.raw_content in prepared.prepared.prompt


def _axis(axis, tilt=0.0):
    return _vector({axis: 1.0, 2: tilt})


def _vector(components):
    vector = [0.0] * EMBEDDING_DIM
    for axis, value in components.items():
        vector[axis] = value
    return vector


async def _embed(content_store, source, vector, space="model-a"):
    await content_store.execute_query(
        "UPDATE raw_captures SET embedding=$vector, metadata.embedding_metadata=$space "
        "WHERE uuid=$uuid;",
        vector=vector,
        space={"model": space},
        uuid=source.id,
    )


async def _family_sources(count, content=lambda index: f"Episode {index}: repair evidence."):
    sources = [
        await remember_raw_memory(
            organization_id="org",
            principal_id="owner",
            source_id=f"family-{index}",
            raw_content=content(index),
            embedding_provider=None,
        )
        for index in range(count)
    ]
    return sorted(sources, key=lambda source: source.id)


def _capacity_in_episodes(monkeypatch, capacity, weights=None, seen=None):
    """Measure a cohort as the sum of its episodes' weights, one each by default."""
    from sibyl_core.config import settings
    from sibyl_core.tasks.ordinary_proposals import PartialCohort

    def size(prepared, *args, **kwargs):
        ids = [
            e.episode_id for e in PartialCohort.model_validate_json(prepared.input_json).episodes
        ]
        if seen is not None:
            seen.append((sorted(ids), prepared.projection_json is not None))
        return sum((weights or {}).get(identifier, 1) for identifier in ids)

    monkeypatch.setattr(settings, "consolidation_max_input_chars", capacity)
    monkeypatch.setattr(service, "_cohort_input_chars", size)


async def test_partition_grows_cohorts_from_nearest_embedded_neighbours(
    cohort_sources, content_store, monkeypatch
):
    sources = await _family_sources(6)
    install_proposal(monkeypatch, sources)
    _capacity_in_episodes(monkeypatch, 3)
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    ids = [source.id for source in sources]
    assert await service.partition_stored_cohort("org", "owner", ids, resolver) == [
        ids[:3],
        ids[3:],
    ]
    # Interleave two families across identifier order, the way unrelated tasks
    # share a page of random identifiers.
    for index, source in enumerate(sources):
        await _embed(content_store, source, _axis(index % 2, tilt=index / 100))
    bins = await service.partition_stored_cohort("org", "owner", ids, resolver)
    assert bins == [ids[0::2], ids[1::2]]
    assert bins == await service.partition_stored_cohort(
        "org", "owner", list(reversed(ids)), resolver
    )


async def test_partition_stops_a_cohort_at_the_family_boundary_despite_spare_budget(
    cohort_sources, content_store, monkeypatch
):
    sources = await _family_sources(6)
    install_proposal(monkeypatch, sources)
    # Room for five: growth alone would top the first family up with the second.
    _capacity_in_episodes(monkeypatch, 5)
    for index, source in enumerate(sources):
        await _embed(content_store, source, _axis(index % 2, tilt=index / 100))
    ids = [source.id for source in sources]
    bins = await service.partition_stored_cohort(
        "org", "owner", ids, AsyncMock(return_value=SourceReadAuthority("owner"))
    )
    assert bins == [ids[0::2], ids[1::2]]


@pytest.mark.parametrize("capacity", [5, 3])
async def test_partition_places_an_unreciprocated_episode_in_its_nearest_cohort(
    cohort_sources, content_store, monkeypatch, capacity
):
    sources = await _family_sources(4)
    install_proposal(monkeypatch, sources)
    _capacity_in_episodes(monkeypatch, capacity)
    # The first identifier seeds first, but every other episode's nearest
    # neighbours are each other, so nothing reciprocates it. It joins the
    # cohort holding its own nearest neighbours when that has room, and stands
    # alone only when the budget says so.
    await _embed(content_store, sources[0], _axis(3, tilt=0.5))
    for index in (1, 2, 3):
        await _embed(content_store, sources[index], _axis(0, tilt=index / 100))
    ids = [source.id for source in sources]
    bins = await service.partition_stored_cohort(
        "org", "owner", ids, AsyncMock(return_value=SourceReadAuthority("owner"))
    )
    assert bins == ([ids[1:] + ids[:1]] if capacity == 5 else [ids[1:], ids[:1]])


async def test_partition_never_places_a_leftover_where_its_neighbours_are_not(
    cohort_sources, content_store, monkeypatch
):
    sources = await _family_sources(8)
    install_proposal(monkeypatch, sources)
    _capacity_in_episodes(monkeypatch, 4)
    # The first identifier is an unreciprocated seed whose nearest neighbours
    # all sit in the first family's cohort, which the budget fills. The second
    # family's cohort has room, but none of the leftover's neighbours, so it
    # stands alone rather than mixing into an unrelated cohort.
    await _embed(content_store, sources[0], _vector({0: 0.6, 3: 0.8}))
    for index in range(1, 5):
        await _embed(content_store, sources[index], _axis(0, tilt=index / 100))
    for index in range(5, 8):
        await _embed(content_store, sources[index], _axis(1, tilt=index / 100))
    ids = [source.id for source in sources]
    bins = await service.partition_stored_cohort(
        "org", "owner", ids, AsyncMock(return_value=SourceReadAuthority("owner"))
    )
    assert bins == [ids[1:5], ids[5:8], ids[:1]]


async def test_partition_ranks_neighbours_among_unplaced_episodes(
    cohort_sources, content_store, monkeypatch
):
    sources = await _family_sources(8)
    install_proposal(monkeypatch, sources)
    _capacity_in_episodes(monkeypatch, 3)
    # Three identical hub episodes, then three spokes whose nearest neighbours
    # are all hubs, then a second family of two. Once the hubs fill the first
    # cohort, the spokes still recognise each other instead of drifting into
    # the other family's cohort.
    for index in (0, 1, 2):
        await _embed(content_store, sources[index], _axis(0))
    for index, spoke in ((3, 3), (4, 4), (5, 5)):
        await _embed(content_store, sources[index], _vector({0: 1.0, spoke: 0.5}))
    for index in (6, 7):
        await _embed(content_store, sources[index], _axis(1, tilt=index / 100))
    ids = [source.id for source in sources]
    bins = await service.partition_stored_cohort(
        "org", "owner", ids, AsyncMock(return_value=SourceReadAuthority("owner"))
    )
    assert bins == [ids[0:3], ids[3:6], ids[6:8]]


async def test_partition_skips_an_oversized_neighbour_without_closing_the_cohort(
    cohort_sources, content_store, monkeypatch
):
    sources = await _family_sources(4)
    install_proposal(monkeypatch, sources)
    ids = [source.id for source in sources]
    # The second episode is every other member's nearest neighbour and alone
    # takes the whole budget; the rest of the family still packs together.
    _capacity_in_episodes(monkeypatch, 10, weights={ids[1]: 10})
    await _embed(content_store, sources[1], _axis(0))
    for index, spoke in ((0, 3), (2, 4), (3, 5)):
        await _embed(content_store, sources[index], _vector({0: 1.0, spoke: 0.3}))
    bins = await service.partition_stored_cohort(
        "org", "owner", ids, AsyncMock(return_value=SourceReadAuthority("owner"))
    )
    assert bins == [[ids[0], ids[2], ids[3]], [ids[1]]]


def _island_vector(group, index):
    """Two islands of one family share axis 0; a third family sits on axis 1."""
    axis = {"a": {0: 1.0, 3: 0.5}, "b": {0: 1.0, 4: 0.5}, "c": {1: 1.0}}[group]
    return _vector({**axis, 2: index / 100})


@pytest.mark.parametrize(("capacity", "joined"), [(8, True), (5, False)])
async def test_partition_joins_two_islands_of_one_family_that_rank_each_other_first(
    cohort_sources, content_store, monkeypatch, capacity, joined
):
    sources = await _family_sources(9)
    install_proposal(monkeypatch, sources)
    _capacity_in_episodes(monkeypatch, capacity)
    # Each island's two nearest neighbours are its own members, so growth
    # closes the family as two cohorts; every member still ranks all five
    # siblings ahead of the other family, which is what joins them.
    for index, source in enumerate(sources):
        await _embed(content_store, source, _island_vector("abc"[index // 3], index))
    ids = [source.id for source in sources]
    bins = await service.partition_stored_cohort(
        "org", "owner", ids, AsyncMock(return_value=SourceReadAuthority("owner"))
    )
    assert bins == ([ids[:6], ids[6:]] if joined else [ids[:3], ids[3:6], ids[6:]])


#: Two islands of one family, an outlier nearer the second island, and a
#: second family. The outlier is a leftover: nothing reciprocates it.
DISPLACEMENT_LAYOUT = (
    [{0: 1.0, 3: 0.33, 2: index / 100} for index in range(3)]
    + [{0: 0.62, 4: 0.45, 5: 0.62}]
    + [{0: 1.0, 4: 0.33, 2: index / 100} for index in range(4, 7)]
    + [{1: 1.0, 2: index / 100} for index in range(7, 10)]
)


@pytest.mark.parametrize(("capacity", "joined"), [(6, False), (8, True)])
async def test_a_join_never_takes_the_room_a_leftover_was_placed_into(
    cohort_sources, content_store, monkeypatch, capacity, joined
):
    sources = await _family_sources(10)
    install_proposal(monkeypatch, sources)
    _capacity_in_episodes(monkeypatch, capacity)
    for source, components in zip(sources, DISPLACEMENT_LAYOUT, strict=True):
        await _embed(content_store, source, _vector(components))
    ids = [source.id for source in sources]
    bins = await service.partition_stored_cohort(
        "org", "owner", ids, AsyncMock(return_value=SourceReadAuthority("owner"))
    )
    # Joining the islands first would fill six places and push the outlier
    # into the other family's cohort; placing it first keeps it beside its
    # own island, and the islands join only when all seven fit.
    family, outlier, other = set(ids[:3] + ids[4:7]), {ids[3]}, set(ids[7:])
    expected = [family | outlier, other] if joined else [set(ids[:3]), set(ids[3:7]), other]
    assert [set(bucket) for bucket in bins] == expected


def _bins(*groups):
    from types import SimpleNamespace

    return [[SimpleNamespace(episode_id=identifier) for identifier in group] for group in groups]


def _scores(groups, pairs, default=0.1):
    """Similarity between every two identifiers, from explicit pair scores."""
    ids = [identifier for group in groups for identifier in group]
    return {
        left: {
            right: pairs.get(frozenset((left, right)), default) for right in ids if right != left
        }
        for left in ids
    }


def _within(*groups, score=0.95):
    return {
        frozenset((left, right)): score
        for group in groups
        for left in group
        for right in group
        if left < right
    }


def _across(left_group, right_group, score):
    return {frozenset((left, right)): score for left in left_group for right in right_group}


def _merged(bins):
    return [[episode.episode_id for episode in bucket] for bucket in bins]


A, B, C, D = ("a0", "a1"), ("b0", "b1"), ("c0", "c1"), ("d0", "d1")


def test_a_merge_needs_every_member_to_rank_the_union_first():
    groups = (A, B, C)
    pairs = {**_within(*groups), **_across(A, B, 0.8)}
    joined = service._merge_separated_bins(_bins(*groups), _scores(groups, pairs), lambda _: True)
    assert _merged(joined) == [[*A, *B], list(C)]

    # One member ranking an outsider ahead of a union sibling keeps them apart,
    # and so does an exact tie across the boundary.
    for outsider in (0.85, 0.8):
        scores = _scores(groups, {**pairs, frozenset(("a0", "c0")): outsider})
        apart = service._merge_separated_bins(_bins(*groups), scores, lambda _: True)
        assert _merged(apart) == [list(A), list(B), list(C)]


def test_two_families_that_each_rank_the_other_first_stay_apart_without_a_margin():
    # Each family is the other's nearest, so every member ranks the union first,
    # but the other family sits nearly as far away as the next outsider.
    groups = (A, B, C)
    pairs = {**_within(*groups, score=0.999), **_across(A, B, 0.88), **_across(A, C, 0.86)}
    pairs.update(_across(B, C, 0.86))
    apart = service._merge_separated_bins(_bins(*groups), _scores(groups, pairs), lambda _: True)
    assert _merged(apart) == [list(A), list(B), list(C)]


@pytest.mark.parametrize(
    ("outsider", "joined"),
    # A and B sit 0.25 apart, so the nearest outsider must be more than 0.5 away.
    [(0.45, True), (0.5, False), (0.6, False)],
)
def test_the_farthest_sibling_must_be_under_half_the_nearest_outsider(outsider, joined):
    groups = (A, B, C)
    pairs = {**_within(*groups), **_across(A, B, 0.75), frozenset(("a0", "c0")): outsider}
    result = service._merge_separated_bins(_bins(*groups), _scores(groups, pairs), lambda _: True)
    assert _merged(result) == ([[*A, *B], list(C)] if joined else [list(A), list(B), list(C)])


def test_every_member_of_both_cohorts_must_stand_apart():
    # Only a member of the second cohort has an outsider close enough to fail.
    groups = (A, B, C)
    pairs = {**_within(*groups), **_across(A, B, 0.8), frozenset(("b1", "c0")): 0.7}
    apart = service._merge_separated_bins(_bins(*groups), _scores(groups, pairs), lambda _: True)
    assert _merged(apart) == [list(A), list(B), list(C)]


def test_an_exact_duplicate_outsider_keeps_a_union_apart():
    """Identical vectors can score a hair above 1; that must not read as distance."""
    above_one = 1.0000000000000002
    groups = (A, B, C)
    pairs = {
        **_within(*groups, score=above_one),
        **_across(A, B, above_one),
        frozenset(("a0", "c0")): above_one,
    }
    apart = service._merge_separated_bins(_bins(*groups), _scores(groups, pairs), lambda _: True)
    assert _merged(apart) == [list(A), list(B), list(C)]


def test_a_cohort_holding_an_episode_without_a_vector_never_joins():
    groups = (A, B, C)
    scores = _scores(groups, {**_within(*groups), **_across(A, B, 0.8)})
    bins = _bins(A, (*B, "x0"), C)
    apart = service._merge_separated_bins(bins, scores, lambda _: True)
    assert _merged(apart) == [list(A), [*B, "x0"], list(C)]


def test_a_merge_needs_an_outsider_to_rank_against():
    """A partition of only the pair cannot tell one family's modes from two families."""
    groups = (A, B)
    scores = _scores(groups, {**_within(*groups), **_across(A, B, 0.8)})
    joined = service._merge_separated_bins(_bins(*groups), scores, lambda _: True)
    assert _merged(joined) == [list(A), list(B)]


def test_the_budget_can_veto_a_merge_but_never_justify_one():
    groups = (A, B, C)
    scores = _scores(groups, {**_within(*groups), **_across(A, B, 0.8)})
    vetoed = service._merge_separated_bins(_bins(*groups), scores, lambda episodes: False)
    assert _merged(vetoed) == [list(A), list(B), list(C)]


def test_a_joined_cohort_never_joins_again_in_the_same_partition():
    # A and B rank each other first; A, B and C together also rank themselves
    # ahead of D. Letting the merged cohort be a candidate would chain C in.
    groups = (A, B, C, D)
    pairs = {
        **_within(*groups),
        **_across(A, B, 0.9),
        **_across(A, C, 0.7),
        **_across(B, C, 0.7),
    }
    joined = service._merge_separated_bins(_bins(*groups), _scores(groups, pairs), lambda _: True)
    assert _merged(joined) == [[*A, *B], list(C), list(D)]


@pytest.mark.parametrize("order", [(A, B, C, D), (C, B, D, A), (D, C, B, A), (B, D, A, C)])
def test_merged_cohorts_do_not_depend_on_bin_order(order):
    pairs = {**_within(*order), **_across(A, B, 0.8)}
    joined = service._merge_separated_bins(_bins(*order), _scores(order, pairs), lambda _: True)
    cohorts = {frozenset(bucket) for bucket in _merged(joined)}
    assert cohorts == {frozenset((*A, *B)), frozenset(C), frozenset(D)}
    # A joined cohort lists its episodes by identifier, the order it is sent in.
    assert [*A, *B] in _merged(joined)


async def test_partition_sizes_a_controller_cohort_as_a_projection_in_a_mixed_group(
    cohort_sources, content_store, monkeypatch
):
    from sibyl_core.tasks._evidence_json import canonical
    from tests.test_episode_evidence import _episode

    def content(index):
        if index == 2:
            return "A plain note about the deploy window."
        episode = _episode()
        episode["goal"] = f"preserve evidence {index}"
        return canonical(episode)

    sources = await _family_sources(3, content)
    install_proposal(monkeypatch, sources)
    seen = []
    _capacity_in_episodes(monkeypatch, 5, seen=seen)
    controllers = [s for s in sources if s.raw_content.startswith("{")]
    for source in controllers:
        await _embed(content_store, source, _axis(0))
    await service.partition_stored_cohort(
        "org",
        "owner",
        [s.id for s in sources],
        AsyncMock(return_value=SourceReadAuthority("owner")),
    )
    # The proposal sends a cohort of controller episodes as a projection, so
    # partitioning must size it that way even though the group is mixed.
    assert (sorted(s.id for s in controllers), True) in seen


async def test_partition_keeps_budget_packing_outside_the_shared_embedding_space(
    cohort_sources, content_store, monkeypatch
):
    sources = await _family_sources(6)
    install_proposal(monkeypatch, sources)
    _capacity_in_episodes(monkeypatch, 3)
    for index in (0, 2, 4):
        await _embed(content_store, sources[index], _axis(0, tilt=index / 100))
    # One vector from another model is not comparable with the shared space.
    await _embed(content_store, sources[1], _axis(0), space="model-b")
    ids = [source.id for source in sources]
    bins = await service.partition_stored_cohort(
        "org", "owner", ids, AsyncMock(return_value=SourceReadAuthority("owner"))
    )
    assert bins == [ids[0::2], ids[1::2]]


async def _memory_model_cohort(monkeypatch, prefix):
    """Twenty 3K-character sources: about 60K characters, over 40K and far under 1.6M."""
    from sibyl_core.config import settings

    sources = [
        await remember_raw_memory(
            organization_id="org",
            principal_id="owner",
            source_id=f"{prefix}-{i}",
            raw_content=f"capture-{i}:" + "a" * 3000,
            embedding_provider=None,
        )
        for i in range(20)
    ]
    install_proposal(monkeypatch, sources)
    owned, _ = await procedure_validation.validation_extractor()
    monkeypatch.setattr(procedure_validation, "validation_extractor", memory_model_factory(owned))
    monkeypatch.setattr(settings, "consolidation_max_input_chars", None)
    return sorted(source.id for source in sources)


def _use_memory_model(monkeypatch, model):
    from sibyl_core.ai.llm import config as llm_config

    monkeypatch.setattr(
        llm_config,
        "_config_source",
        llm_config.EnvConfigSource(
            {"SIBYL_LLM_MEMORY_PROVIDER": "anthropic", "SIBYL_LLM_MEMORY_MODEL": model}
        ),
    )


@pytest.mark.parametrize(
    ("model", "whole"), [("claude-opus-5-5", True), ("claude-haiku-4-5", False)]
)
async def test_partition_uses_the_memory_models_budget_when_none_is_set(
    cohort_sources, content_store, monkeypatch, model, whole
):
    ids = await _memory_model_cohort(monkeypatch, "model-budget")
    _use_memory_model(monkeypatch, model)
    bins = await service.partition_stored_cohort(
        "org", "owner", ids, AsyncMock(return_value=SourceReadAuthority("owner"))
    )
    assert sorted(i for bucket in bins for i in bucket) == ids
    assert (len(bins) == 1) is whole


async def test_proposal_admits_a_cohort_over_40k_on_the_opus_default(
    cohort_sources, content_store, monkeypatch
):
    ids = await _memory_model_cohort(monkeypatch, "opus-proposal")
    _use_memory_model(monkeypatch, "claude-opus-5-5")
    candidate, _ = await service.propose_stored_cohort(
        "org",
        "owner",
        ids,
        AsyncMock(return_value=SourceReadAuthority("owner")),
        authorize=AsyncMock(),
    )
    assert candidate is not None
    stage = (await content_store.execute_query("SELECT * FROM memory_validation_executions;"))[0]
    packing = json.loads(stage["request_json"])["policy"]
    assert json.loads(packing)["max_input_chars"] == 1_600_000


async def test_a_model_change_after_partition_refuses_the_cohort_before_any_send(
    cohort_sources, content_store, monkeypatch
):
    from sibyl_core.tasks.consolidation import ConsolidationInputBudgetExceeded

    ids = await _memory_model_cohort(monkeypatch, "model-change")
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    _use_memory_model(monkeypatch, "claude-opus-5-5")
    assert await service.partition_stored_cohort("org", "owner", ids, resolver) == [ids]

    _use_memory_model(monkeypatch, "claude-haiku-4-5")
    monkeypatch.setattr(
        Extractor, "extract_with_usage", AsyncMock(side_effect=AssertionError("sent"))
    )
    with pytest.raises(ConsolidationInputBudgetExceeded) as refused:
        await service.propose_stored_cohort("org", "owner", ids, resolver, authorize=AsyncMock())
    assert refused.value.max_input_chars == 40_000
    assert "the configured limit for claude-haiku-4-5 is 40000 characters" in str(refused.value)
    assert await content_store.execute_query("SELECT * FROM memory_validation_executions;") == []


async def test_cohort_partition_keeps_event_loop_responsive(cohort_sources, monkeypatch):
    install_proposal(monkeypatch, cohort_sources)
    loop = asyncio.get_running_loop()
    responsive = Event()
    original = service._cohort_input_chars

    def count_input(*args, **kwargs):
        loop.call_soon_threadsafe(responsive.set)
        assert responsive.wait(2), "cohort partition blocked its event loop"
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_cohort_input_chars", count_input)
    ids = sorted(source.id for source in cohort_sources)
    result = await service.partition_stored_cohort(
        "org", "owner", ids, AsyncMock(return_value=SourceReadAuthority("owner"))
    )
    assert responsive.is_set()
    assert result == [ids]


async def test_cancelled_partition_stops_before_preparing_another_fit(cohort_sources, monkeypatch):
    sources = [
        await remember_raw_memory(
            organization_id="org",
            principal_id="owner",
            source_id=f"cancel-{index}",
            raw_content=f"Capture {index}: preserve source evidence.",
            embedding_provider=None,
        )
        for index in range(4)
    ]
    install_proposal(monkeypatch, sources)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    stopped = asyncio.Event()
    release = Event()
    calls = []
    original_count = service._cohort_input_chars
    original_partition = service._partition_prepared_cohort

    def count_input(*args, **kwargs):
        calls.append(True)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "partition control was not released"
        return original_count(*args, **kwargs)

    def partition(*args):
        try:
            return original_partition(*args)
        finally:
            loop.call_soon_threadsafe(stopped.set)

    monkeypatch.setattr(service, "_cohort_input_chars", count_input)
    monkeypatch.setattr(service, "_partition_prepared_cohort", partition)
    task = asyncio.create_task(
        service.partition_stored_cohort(
            "org",
            "owner",
            [source.id for source in sources],
            AsyncMock(return_value=SourceReadAuthority("owner")),
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
    await asyncio.wait_for(stopped.wait(), timeout=5)
    assert len(calls) == 1


async def test_partition_worker_preserves_preparation_failure(cohort_sources, monkeypatch):
    install_proposal(monkeypatch, cohort_sources)

    def fail_preparation(*args, **kwargs):
        raise ValueError("source evidence cannot be prepared")

    monkeypatch.setattr(service, "_cohort_input_chars", fail_preparation)
    with pytest.raises(ValueError, match="source evidence cannot be prepared"):
        await service.partition_stored_cohort(
            "org",
            "owner",
            [source.id for source in cohort_sources],
            AsyncMock(return_value=SourceReadAuthority("owner")),
        )


def _coverage_row(uuid, sources, *, procedure=True, error=None, **request):
    bindings = [{"source_id": s, "incarnation": f"inc-{s}", "generation": 1} for s in sources]
    return {
        "uuid": uuid,
        "principal_id": "owner",
        "request_json": json.dumps(
            {"kind": service.VERSION, "source_bindings": bindings, **request}
        ),
        "result_json": json.dumps(
            {
                "proposal": {"procedure": {"kind": "pattern"} if procedure else None},
                "validation_error": error,
            }
        ),
    }


async def test_completed_cohorts_cover_only_sources_whose_proposal_finished(monkeypatch):
    from sibyl_core.services.validation_candidate import ValidationCandidateWrite

    def candidate(uuid):
        return ValidationCandidateWrite(uuid, "", "", {}).id

    rows = [
        _coverage_row("abstained", ["a1", "a2"], procedure=False),
        _coverage_row("stored", ["s1", "s2"]),
        _coverage_row("retired", ["r1", "r2"]),
        # Returned, but the candidate was never written: replay must stay possible.
        _coverage_row("unwritten", ["u1", "u2"]),
        _coverage_row("invalid", ["i1", "i2"], procedure=False, error="bad support"),
        _coverage_row("packet", ["p1", "p2"], procedure=False, evidence_packet={}),
        _coverage_row("single", ["o1"], procedure=False),
        _coverage_row("other-kind", ["k1", "k2"], procedure=False, kind="other"),
    ]
    stored, retired = [{"uuid": candidate("stored")}], [{"source_id": candidate("retired")}]

    async def query(statement, **params):
        if statement == service.COHORT_COVERAGE:
            assert params == {"org": "org", "kind": service.VERSION}
            return rows
        assert params["keys"] == [
            {"org": "org", "id": identifier}
            for identifier in sorted(candidate(uuid) for uuid in ("stored", "retired", "unwritten"))
        ]
        return stored if statement == service.COHORT_CANDIDATES_STORED else retired

    monkeypatch.setattr(service, "_query", query)
    covered = await service.completed_cohort_sources("org")
    assert covered == {
        ("owner", source, f"inc-{source}", 1) for source in ("a1", "a2", "s1", "s2", "r1", "r2")
    }


async def test_the_dream_reads_use_their_indexes(content_store):
    from tests.test_validation_dependencies import assert_index

    execution = await content_store.execute_query(
        service.COHORT_COVERAGE.replace(";", " EXPLAIN;"), org="org", kind=service.VERSION
    )
    assert_index(execution, "memory_validation_execution_org_state")
    checkpoint = await content_store.execute_query(
        "SELECT source_id, request_json FROM dream_source_checkpoints "
        "WHERE organization_id = $org AND completion_json != NONE EXPLAIN;",
        org="org",
    )
    assert_index(checkpoint, "dream_checkpoint_source")
