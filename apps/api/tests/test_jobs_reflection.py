from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from sibyl.jobs.reflection import _candidate_target_scope_key, run_reflection_dream_cycle
from sibyl_core.models.reflection import ReflectionPack
from sibyl_core.services.memory import (
    ReflectionPromotionPreview,
    ReflectionPromotionResult,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory


@pytest.fixture(autouse=True)
def dispatch_cursor(monkeypatch):
    # Load the consumer before patching its dependency so cached imports stay real.
    from sibyl_core.services import automatic_reflection  # noqa: F401

    monkeypatch.setattr("sibyl.jobs.reflection.load_dream_cursor", AsyncMock(return_value=("", 0)))
    monkeypatch.setattr("sibyl.jobs.reflection.advance_dream_cursor", AsyncMock(return_value=True))
    from sibyl_core.services.ordinary_cohort import ReflectedSources

    monkeypatch.setattr(
        "sibyl.jobs.reflection.reflected_sources",
        AsyncMock(return_value=ReflectedSources(frozenset(), frozenset())),
    )
    monkeypatch.setattr(
        "sibyl.jobs.reflection.completed_dream_sources", AsyncMock(return_value=frozenset())
    )
    monkeypatch.setattr(
        "sibyl.jobs.reflection.current_source_observations", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        "sibyl.jobs.reflection.list_reflection_dream_neighbours", AsyncMock(return_value=[])
    )
    # These orchestration tests stub the validator; the public cohort tests
    # exercise the durable binding and real publisher together.
    monkeypatch.setattr(
        "sibyl_core.services.ordinary_publication.ordinary_promotion_binding",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "sibyl_core.services.reflection_validation.prepare_stored_reflection",
        AsyncMock(
            return_value=SimpleNamespace(sources=[], memory=SimpleNamespace(review_state="pending"))
        ),
    )


ORG_ID = "00000000-0000-0000-0000-000000000111"
USER_ID = "00000000-0000-0000-0000-000000000222"


def _raw_memory(**overrides: object) -> RawMemory:
    values = {
        "id": "source-1",
        "organization_id": ORG_ID,
        "source_id": "cli:manual",
        "principal_id": USER_ID,
        "memory_scope": MemoryScope.PRIVATE,
        "scope_key": None,
        "project_id": None,
        "review_state": "pending",
        "entity_type": "raw_memory",
        "title": "Session notes",
        "raw_content": "We decided reflection should run automatically.",
        "tags": ["memory"],
        "metadata": {"domain": "sibyl"},
        "provenance": {},
        "capture_surface": "cli",
        "captured_at": datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
        "created_at": datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
        "score": 0.0,
    }
    values.update(overrides)
    return RawMemory(**values)


def _reflection_pack(*, persisted_count: int = 1) -> ReflectionPack:
    return ReflectionPack(
        source_title="Session notes",
        source_id="source-1",
        intent="maintenance",
        domain="sibyl",
        project=None,
        candidates=[SimpleNamespace()],
        total_candidates=1,
        persisted_count=persisted_count,
    )


def _preview(
    *,
    candidate_id: str = "candidate-1",
    metadata: dict[str, object] | None = None,
) -> ReflectionPromotionPreview:
    return ReflectionPromotionPreview(
        allowed=True,
        candidate_id=candidate_id,
        reason="promotion_preview_allowed",
        review_state="pending",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        raw_source_ids=["source-1"],
        metadata={"confidence": 0.94, **(metadata or {})},
    )


def _promotion(candidate_id: str = "candidate-1") -> ReflectionPromotionResult:
    return ReflectionPromotionResult(
        success=True,
        candidate_id=candidate_id,
        promoted_id="promoted-1",
        reason="accepted_reflection_candidate",
        review_state="promoted",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        raw_source_ids=["source-1"],
    )


def test_team_scope_nomination_does_not_reuse_project_scope_key() -> None:
    candidate = _raw_memory(
        memory_scope=MemoryScope.PROJECT,
        scope_key="project-123",
        project_id="project-123",
        metadata={"suggested_memory_scope": "team"},
    )

    assert _candidate_target_scope_key(candidate, MemoryScope.TEAM.value) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("denial_reason", [None, "retired", "promotion_incomplete"])
async def test_reflection_dream_cycle_reports_actual_promotion(denial_reason) -> None:
    source = _raw_memory(
        id="source-1",
        metadata={"suggested_memory_scope": "team"},
    )
    candidate = _raw_memory(
        id="candidate-1",
        capture_surface="reflection_candidate",
        raw_content="Reflection should run automatically.",
        metadata={"suggested_memory_scope": "private", "confidence": 0.94},
    )

    with (
        patch(
            "sibyl_core.services.automatic_reflection.automatically_review_reflection",
            AsyncMock(
                return_value=SimpleNamespace(
                    candidate=candidate, executions=("critic-fixture",), candidate_ids=()
                )
            ),
        ),
        patch(
            "sibyl.jobs.reflection.list_reflection_dream_source_memories",
            AsyncMock(return_value=[source]),
        ),
        patch(
            "sibyl.jobs.reflection.reflect_memory",
            AsyncMock(return_value=_reflection_pack()),
        ) as reflect,
        patch("sibyl.jobs.reflection.save_raw_memory", AsyncMock(return_value=source)) as save,
        patch(
            "sibyl.jobs.reflection.list_reflection_candidate_reviews",
            AsyncMock(return_value=[candidate]),
        ),
        patch(
            "sibyl.jobs.reflection.resolve_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "sibyl.jobs.reflection.preview_reflection_candidate_promotion",
            AsyncMock(return_value=_preview()),
        ),
        patch(
            "sibyl.jobs.reflection.promote_reflection_candidate_review",
            AsyncMock(
                return_value=replace(
                    _promotion(), success=False, reason=denial_reason, review_state="pending"
                )
                if denial_reason
                else _promotion()
            ),
        ) as promote,
        patch("sibyl.jobs.reflection.log_memory_audit_event", AsyncMock()) as audit,
    ):
        receipt = await run_reflection_dream_cycle(
            {},
            ORG_ID,
            source_limit=1,
            candidate_limit=1,
        )

    reflect.assert_awaited_once()
    reflect_kwargs = reflect.await_args.kwargs
    assert reflect_kwargs["existing_source_id"] == "source-1"
    assert reflect_kwargs["persist"] is True
    assert reflect_kwargs["persist_source"] is False
    assert reflect_kwargs["persist_review"] is True
    assert reflect_kwargs["suggested_memory_scope"] == "team"
    assert reflect_kwargs["suggested_scope_key"] is None
    promote.assert_awaited_once()
    assert save.await_count == 1
    assert audit.await_count == 1
    assert receipt["sources_reflected"] == 1
    assert receipt["promoted"] == (0 if denial_reason else 1)
    assert receipt["failed"] == (1 if denial_reason else 0)
    reported = receipt["candidates"][0]
    assert reported["applied"] is (denial_reason is None)
    assert reported["outcome"] == ("error" if denial_reason else "auto_promote")
    assert reported["promoted_id"] == (None if denial_reason else "promoted-1")
    assert reported["recommended_action"] == "promote"
    if denial_reason:
        assert reported["reason"] == denial_reason
    audit_kwargs = audit.await_args.kwargs
    assert audit_kwargs["action"] == (
        "memory.reflect.dream_review" if denial_reason else "memory.reflect.dream_promote"
    )
    assert audit_kwargs["details"]["outcome"] == reported["outcome"]
    assert audit_kwargs["policy_reason"] == reported["reason"]
    assert audit_kwargs["derived_ids"] == ([] if denial_reason else ["promoted-1"])


@pytest.mark.asyncio
async def test_reflection_dream_cycle_dry_run_writes_no_memory() -> None:
    source = _raw_memory(id="source-1")
    candidate = _raw_memory(
        id="candidate-1",
        capture_surface="reflection_candidate",
        metadata={"suggested_memory_scope": "private", "confidence": 0.94},
    )

    with (
        patch(
            "sibyl_core.services.automatic_reflection.automatically_review_reflection",
            AsyncMock(
                return_value=SimpleNamespace(
                    candidate=candidate, executions=("critic-fixture",), candidate_ids=()
                )
            ),
        ),
        patch(
            "sibyl.jobs.reflection.list_reflection_dream_source_memories",
            AsyncMock(return_value=[source]),
        ),
        patch(
            "sibyl.jobs.reflection.reflect_memory",
            AsyncMock(return_value=_reflection_pack(persisted_count=0)),
        ) as reflect,
        patch("sibyl.jobs.reflection.save_raw_memory", AsyncMock()) as save,
        patch(
            "sibyl.jobs.reflection.list_reflection_candidate_reviews",
            AsyncMock(return_value=[candidate]),
        ),
        patch(
            "sibyl.jobs.reflection.resolve_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "sibyl.jobs.reflection.preview_reflection_candidate_promotion",
            AsyncMock(return_value=_preview()),
        ),
        patch("sibyl.jobs.reflection.promote_reflection_candidate_review", AsyncMock()) as promote,
        patch("sibyl.jobs.reflection.log_memory_audit_event", AsyncMock()) as audit,
    ):
        receipt = await run_reflection_dream_cycle(
            {},
            ORG_ID,
            dry_run=True,
            source_limit=1,
            candidate_limit=1,
        )

    reflect_kwargs = reflect.await_args.kwargs
    assert reflect_kwargs["persist"] is False
    assert reflect_kwargs["persist_review"] is False
    save.assert_not_awaited()
    promote.assert_not_awaited()
    assert receipt["dry_run"] is True
    assert receipt["promoted"] == 0
    assert receipt["failed"] == 0
    assert receipt["candidates"][0]["outcome"] == "auto_promote"
    assert receipt["candidates"][0]["recommended_action"] == "promote"
    assert receipt["candidates"][0]["applied"] is False
    assert receipt["archived"] == 0

    assert audit.await_args.kwargs["action"] == "memory.reflect.dream_review"
    assert audit.await_args.kwargs["derived_ids"] == []


@pytest.mark.asyncio
async def test_reflection_dream_cycle_archives_terminal_exception_candidates() -> None:
    candidate = _raw_memory(
        id="candidate-duplicate",
        capture_surface="reflection_candidate",
        metadata={
            "candidate_duplicate_of_source_id": "source-0",
            "suggested_memory_scope": "private",
            "confidence": 0.94,
        },
    )
    archived = _raw_memory(
        id="candidate-duplicate",
        capture_surface="reflection_candidate",
        review_state="archived",
    )

    with (
        patch(
            "sibyl_core.services.automatic_reflection.automatically_review_reflection",
            AsyncMock(
                return_value=SimpleNamespace(
                    candidate=candidate, executions=("critic-fixture",), candidate_ids=()
                )
            ),
        ),
        patch(
            "sibyl.jobs.reflection.list_reflection_dream_source_memories",
            AsyncMock(return_value=[]),
        ),
        patch("sibyl.jobs.reflection.save_raw_memory", AsyncMock(return_value=archived)) as save,
        patch(
            "sibyl.jobs.reflection.list_reflection_candidate_reviews",
            AsyncMock(return_value=[candidate]),
        ),
        patch(
            "sibyl.jobs.reflection.resolve_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "sibyl.jobs.reflection.preview_reflection_candidate_promotion",
            AsyncMock(
                return_value=_preview(
                    candidate_id="candidate-duplicate",
                    metadata={"candidate_duplicate_of_source_id": "source-0"},
                )
            ),
        ),
        patch("sibyl.jobs.reflection.promote_reflection_candidate_review", AsyncMock()) as promote,
        patch("sibyl.jobs.reflection.log_memory_audit_event", AsyncMock()),
    ):
        receipt = await run_reflection_dream_cycle(
            {},
            ORG_ID,
            dry_run=False,
            source_limit=0,
            candidate_limit=1,
        )

    promote.assert_not_awaited()
    save.assert_awaited_once()
    saved_memory = save.await_args.args[0]
    assert saved_memory.review_state == "archived"
    assert receipt["archived"] == 1
    assert receipt["exceptioned"] == 0
    assert receipt["candidates"][0]["outcome"] == "abstained"
    assert receipt["candidates"][0]["recommended_action"] == "abstain"
    assert receipt["candidates"][0]["exception_reasons"] == ["duplicate_candidate"]


@pytest.mark.parametrize("archive_exceptions", [False, True])
async def test_dream_worker_accepts_legacy_archive_payload(archive_exceptions):
    result = await run_reflection_dream_cycle(
        {},
        ORG_ID,
        source_limit=0,
        candidate_limit=0,
        archive_exceptions=archive_exceptions,
        archive_exception_reasons=["duplicate_candidate"],
    )
    assert result["sources_scanned"] == 0
    assert result["candidates_scanned"] == 0
    assert result["failed"] == 0


@pytest.mark.asyncio
async def test_reflection_dream_cycle_retires_superseded_drafts() -> None:
    """A draft whose corrected child is published reaches a terminal state."""
    draft = _raw_memory(
        id="draft-1",
        capture_surface="reflection_candidate",
        metadata={"suggested_memory_scope": "private", "confidence": 0.94},
    )
    frontier = _raw_memory(
        id="child-1",
        capture_surface="reflection_candidate",
        review_state="promoted",
        metadata={"suggested_memory_scope": "private", "confidence": 0.94},
    )

    with (
        patch(
            "sibyl_core.services.automatic_reflection.automatically_review_reflection",
            AsyncMock(
                return_value=SimpleNamespace(
                    candidate=frontier,
                    executions=("critic-fixture",),
                    candidate_ids=("draft-1", "child-1"),
                )
            ),
        ),
        patch(
            "sibyl_core.services.reflection_validation.prepare_stored_reflection",
            AsyncMock(
                return_value=SimpleNamespace(
                    sources=[], memory=SimpleNamespace(review_state="promoted")
                )
            ),
        ),
        patch(
            "sibyl_core.services.reflection_supersession.retire_superseded_reflection_drafts",
            AsyncMock(return_value=["draft-1"]),
        ) as retire,
        patch(
            "sibyl.jobs.reflection.list_reflection_dream_source_memories",
            AsyncMock(return_value=[]),
        ),
        patch(
            "sibyl.jobs.reflection.list_reflection_candidate_reviews",
            AsyncMock(side_effect=[[draft], []]),
        ),
        patch(
            "sibyl.jobs.reflection.resolve_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "sibyl.jobs.reflection.preview_reflection_candidate_promotion",
            AsyncMock(return_value=_preview(candidate_id="child-1")),
        ),
        patch("sibyl.jobs.reflection.promote_reflection_candidate_review", AsyncMock()) as promote,
        patch("sibyl.jobs.reflection.log_memory_audit_event", AsyncMock()),
    ):
        receipt = await run_reflection_dream_cycle(
            {},
            ORG_ID,
            source_limit=0,
            candidate_limit=5,
        )

    promote.assert_not_awaited()
    retire.assert_awaited_once()
    assert retire.await_args.kwargs == {
        "organization_id": ORG_ID,
        "promoted_candidate_id": "child-1",
    }
    reported = receipt["candidates"][0]
    assert reported["outcome"] == "skip"
    assert reported["reason"] == "candidate_already_promoted"
    assert reported["superseded_retired"] == ["draft-1"]
    assert receipt["superseded_retired"] == 1


@pytest.mark.asyncio
async def test_reflection_dream_cycle_leaves_open_drafts_pending() -> None:
    """Only a published frontier retires a draft, so open work keeps its slot."""
    draft = _raw_memory(
        id="draft-1",
        capture_surface="reflection_candidate",
        metadata={"suggested_memory_scope": "private", "confidence": 0.94},
    )

    with (
        patch(
            "sibyl_core.services.automatic_reflection.automatically_review_reflection",
            AsyncMock(
                return_value=SimpleNamespace(
                    candidate=draft, executions=("critic-fixture",), candidate_ids=("draft-1",)
                )
            ),
        ),
        patch(
            "sibyl_core.services.reflection_supersession.retire_superseded_reflection_drafts",
            AsyncMock(return_value=[]),
        ) as retire,
        patch(
            "sibyl.jobs.reflection.list_reflection_dream_source_memories",
            AsyncMock(return_value=[]),
        ),
        patch(
            "sibyl.jobs.reflection.list_reflection_candidate_reviews",
            AsyncMock(side_effect=[[draft], []]),
        ),
        patch(
            "sibyl.jobs.reflection.resolve_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "sibyl.jobs.reflection.preview_reflection_candidate_promotion",
            AsyncMock(return_value=_preview(candidate_id="draft-1")),
        ),
        patch(
            "sibyl.jobs.reflection.promote_reflection_candidate_review",
            AsyncMock(return_value=_promotion("draft-1")),
        ),
        patch("sibyl.jobs.reflection.log_memory_audit_event", AsyncMock()),
    ):
        receipt = await run_reflection_dream_cycle(
            {},
            ORG_ID,
            source_limit=0,
            candidate_limit=5,
        )

    retire.assert_not_awaited()
    assert receipt["candidates"][0]["outcome"] == "auto_promote"
    assert receipt["superseded_retired"] == 0


@pytest.mark.asyncio
async def test_dream_drain_visits_every_pending_candidate_once() -> None:
    """A short page is not the last page, so no pending candidate is stranded."""
    from sibyl.jobs.reflection import _drain_dream_candidates

    pending = [
        _raw_memory(
            id=f"candidate-{index}",
            capture_surface="reflection_candidate",
            captured_at=datetime(2026, 5, 15, 12, index, tzinfo=UTC),
        )
        for index in range(7)
    ]
    # Today's reader always fills a page it can fill. These shapes describe a
    # reader the drain has to survive rather than one it currently meets: the
    # contract under test is that page length is never read as an end signal,
    # so a future filter cannot strand candidates behind a short page.
    pages = [pending[0:2], pending[2:3], pending[3:7], []]
    visited: list[str] = []

    async def _reader(*, organization_id, review_state, limit, after):
        assert review_state == "pending"
        return pages.pop(0) if pages else []

    async def _handle(*, candidate, **_kwargs):
        visited.append(candidate.id)
        return {"candidate_id": candidate.id, "outcome": "skip", "reason": "x"}

    with (
        patch("sibyl.jobs.reflection.list_reflection_candidate_reviews", _reader),
        patch("sibyl.jobs.reflection._drain_dream_candidate", _handle),
    ):
        results = await _drain_dream_candidates(
            group_id=ORG_ID,
            run_id="run-1",
            dry_run=False,
            limit=50,
            confidence_threshold=None,
        )

    assert visited == [memory.id for memory in pending]
    assert len(results) == len(pending)


def _walk(count: int) -> list[RawMemory]:
    return [_raw_memory(id=f"walk-{index}") for index in range(count)]


@pytest.mark.parametrize(
    ("neighbour_ids", "page_ids", "walked_ids"),
    [
        # A neighbour beyond the walk prefix keeps its place but never moves
        # the cursor; the walk stops at the first source without room.
        (["walk-3", "outside"], ["walk-0", "walk-1", "walk-3", "outside"], ["walk-0", "walk-1"]),
        # A neighbour inside the prefix takes no extra room, so the walk runs on.
        (
            ["walk-1", "outside"],
            ["walk-0", "walk-1", "walk-2", "outside"],
            ["walk-0", "walk-1", "walk-2"],
        ),
        # A seed without neighbours pages exactly as the plain walk did.
        ([], ["walk-0", "walk-1", "walk-2", "walk-3"], ["walk-0", "walk-1", "walk-2", "walk-3"]),
    ],
)
async def test_dream_page_holds_a_walk_prefix_and_the_seed_neighbours(
    monkeypatch, neighbour_ids, page_ids, walked_ids
) -> None:
    from sibyl.jobs import reflection

    walk = _walk(5)
    known = {source.id: source for source in [*walk, _raw_memory(id="outside")]}
    neighbours = AsyncMock(return_value=[known[identifier] for identifier in neighbour_ids])
    monkeypatch.setattr(reflection, "list_reflection_dream_neighbours", neighbours)
    pending = AsyncMock(return_value=True)

    page, walked = await reflection._dream_page(ORG_ID, walk, 4, pending)

    assert [source.id for source in page] == page_ids
    assert walked == set(walked_ids)
    neighbours.assert_awaited_once_with(
        organization_id=ORG_ID, seed=walk[0], limit=3, is_pending=pending, prefetch=None
    )
    assert await reflection._dream_page(ORG_ID, [], 4, pending) == ([], set())


async def test_dream_cursor_advances_through_the_walk_prefix_in_walk_order(monkeypatch) -> None:
    from sibyl.jobs import ordinary_cohorts, reflection

    walk = _walk(5)
    outside = _raw_memory(id="outside")
    monkeypatch.setattr(
        reflection, "list_reflection_dream_source_memories", AsyncMock(return_value=walk)
    )
    monkeypatch.setattr(
        reflection,
        "list_reflection_dream_neighbours",
        AsyncMock(return_value=[walk[3], outside]),
    )
    page_ids = {"walk-0", "walk-1", "walk-3", "outside"}
    reflect = AsyncMock(return_value=([], page_ids))
    monkeypatch.setattr(ordinary_cohorts, "reflect_cohorts", reflect)

    await reflection._reflect_dream_sources(group_id=ORG_ID, run_id="run", dry_run=False, limit=4)

    assert {source.id for source in reflect.await_args.args[1]} == page_ids
    assert reflection.advance_dream_cursor.await_args_list == [
        ((ORG_ID, "walk-0", 0),),
        ((ORG_ID, "walk-1", 1),),
    ]


class _DreamCorpus:
    """An in-memory stand-in for the dream job's store, cursor and passes."""

    def __init__(self, stale: int, fresh: int) -> None:
        rng = random.Random(20)  # noqa: S311 - reproducible identifiers, not secrets
        ids = sorted(f"{rng.getrandbits(64):016x}" for _ in range(stale + fresh))
        picked = set(rng.sample(ids, stale))
        self.sources = {identifier: _raw_memory(id=identifier) for identifier in ids}
        self.order = ids
        # Reflected alone at their current observation, and nearest to every seed.
        self.stale = [identifier for identifier in ids if identifier in picked]
        self.fresh = [identifier for identifier in ids if identifier not in picked]
        self.covered: set[str] = set()
        self.cursor, self.revision = "", 0
        self.rechecked: list[str] = []
        self.pages: list[list[str]] = []
        self.advances: list[int] = []

    def key(self, identifier: str) -> tuple[str, str, str, int]:
        return (USER_ID, identifier, "inc", 1)

    async def walk(self, *, organization_id, limit, is_pending, after_source_id, prefetch):
        after = [i for i in self.order if i > after_source_id]
        before = [i for i in self.order if i <= after_source_id]
        memories = [self.sources[identifier] for identifier in after + before]
        result = []
        for start in range(0, len(memories), 50):
            batch = memories[start : start + 50]
            await prefetch(batch)
            for memory in batch:
                if await is_pending(memory):
                    result.append(memory)
                    if len(result) == limit:
                        return result
        return result

    async def neighbours(self, *, organization_id, seed, limit, is_pending, prefetch):
        ranked = [self.sources[i] for i in [*self.stale, *self.fresh] if i != seed.id]
        await prefetch(ranked)
        result = []
        for memory in ranked:
            if await is_pending(memory):
                result.append(memory)
                if len(result) == limit:
                    break
        return result

    async def observations(self, organization_id, source_ids):
        return dict.fromkeys(source_ids, ("inc", 1))

    async def reflected(self, organization_id):
        from sibyl_core.services.ordinary_cohort import ReflectedSources

        return ReflectedSources(frozenset(map(self.key, self.covered)), frozenset())

    async def completed(self, organization_id):
        return frozenset(map(self.key, self.stale))

    async def load_work(self, group_id, source):
        if source.id in self.covered:
            self.rechecked.append(source.id)
        observation = SimpleNamespace(effective_incarnation="inc", generation=1)
        return SimpleNamespace(snapshot=SimpleNamespace(observation=observation, memory=source))

    async def reflect(self, org, sources, *, dry_run):
        page = [source.id for source in sources]
        self.pages.append(page)
        # Every fresh source on a page completes a cohort; stale ones never do.
        self.covered.update(identifier for identifier in page if identifier not in self.stale)
        return [], set(page)

    async def load_cursor(self, organization_id):
        return self.cursor, self.revision

    async def advance(self, organization_id, source_id, revision):
        self.cursor, self.revision = source_id, revision + 1
        self.advances[-1] += 1
        return True

    def install(self, monkeypatch) -> None:
        from sibyl.jobs import ordinary_cohorts, reflection

        for name, fake in (
            ("list_reflection_dream_source_memories", self.walk),
            ("list_reflection_dream_neighbours", self.neighbours),
            ("current_source_observations", self.observations),
            ("reflected_sources", self.reflected),
            ("completed_dream_sources", self.completed),
            ("_load_dream_work", self.load_work),
            ("load_dream_cursor", self.load_cursor),
            ("advance_dream_cursor", self.advance),
            ("load_dream_stage", AsyncMock(return_value={"completion_json": "{}"})),
        ):
            monkeypatch.setattr(reflection, name, fake)
        monkeypatch.setattr(ordinary_cohorts, "reflect_cohorts", self.reflect)
        from sibyl_core.services.memory_source_validation import SourceReadAuthority

        monkeypatch.setattr(
            ordinary_cohorts,
            "writable_source_authority",
            AsyncMock(return_value=SourceReadAuthority(USER_ID)),
        )
        monkeypatch.setattr(reflection, "observe_raw_capture", lambda memory, authority: None)


async def test_sources_reflected_alone_cannot_stall_the_cursor(monkeypatch) -> None:
    """A crowd of sources reflected alone near every seed leaves the walk moving.

    Fresh sources rank in walk order here, isolating the crowding. A fresh
    neighbour from beyond the cursor is progress without moving the cursor,
    which the fresh-paged floor covers.
    """
    from sibyl.jobs import reflection

    limit = 20
    corpus = _DreamCorpus(stale=2 * limit, fresh=6 * limit)
    corpus.install(monkeypatch)
    while set(corpus.fresh) - corpus.covered:
        assert len(corpus.pages) < 20, "the fresh backlog stopped draining"
        remaining = len(set(corpus.fresh) - corpus.covered)
        corpus.advances.append(0)
        await reflection._reflect_dream_sources(
            group_id=ORG_ID, run_id="run", dry_run=False, limit=limit
        )
        page = corpus.pages[-1]
        # A source already reflected alone never seeds a page or fills it from
        # the walk; it returns only beside a fresh seed, and at most half a
        # page of them.
        assert page[0] in corpus.fresh
        assert sum(identifier in corpus.stale for identifier in page) <= limit // 2
        if remaining >= limit:
            assert corpus.advances[-1] >= limit // 2, corpus.advances
            paged = remaining - len(set(corpus.fresh) - corpus.covered)
            assert paged >= limit // 2, (paged, page)
    assert len(corpus.pages) <= 2 * len(corpus.fresh) // limit
    # A covered source is ruled out by its observation, never authorized again.
    assert corpus.rechecked == []


def _selection(monkeypatch, send, *, returning=10):
    """A run's selection whose snapshot load succeeds and whose send gate is `send`."""
    from sibyl.jobs import ordinary_cohorts, reflection
    from sibyl_core.services.ordinary_cohort import ReflectedSources

    async def load_work(group_id, source):
        observation = SimpleNamespace(effective_incarnation="inc", generation=1)
        return SimpleNamespace(snapshot=SimpleNamespace(observation=observation, memory=source))

    monkeypatch.setattr(reflection, "_load_dream_work", load_work)
    monkeypatch.setattr(ordinary_cohorts, "writable_source_authority", send)
    return reflection._DreamSelection(
        ORG_ID, ReflectedSources(frozenset(), frozenset()), frozenset(), returning=returning
    )


async def test_selection_refuses_a_project_source_its_owner_can_no_longer_send(
    monkeypatch,
) -> None:
    from sibyl_core.services.memory_source_validation import SourceReadAuthority

    # Demoted to viewer on "ops": still readable, so the read gate passes, but
    # the send authority keeps only projects its owner can still write.
    send = AsyncMock(return_value=SourceReadAuthority(USER_ID, projects=frozenset({"docs"})))
    selection = _selection(monkeypatch, send)
    stamp = {"revision": 1, "observed_revision": 1}
    ops = _raw_memory(
        id="ops", memory_scope=MemoryScope.PROJECT, project_id="ops", scope_key="ops", **stamp
    )
    docs = _raw_memory(
        id="docs", memory_scope=MemoryScope.PROJECT, project_id="docs", scope_key="docs", **stamp
    )
    private = _raw_memory(id="private", **stamp)

    assert await selection.fresh(ops) is False
    assert await selection.authorize(ops) == "unavailable"
    assert await selection.fresh(docs) is True
    assert await selection.fresh(private) is True
    assert set(selection.selected) == {"docs", "private"}
    assert selection.unsendable == 1
    # One send-gate resolution per principal per run.
    send.assert_awaited_once_with(ORG_ID, USER_ID)


@pytest.mark.parametrize("refusal", ["unavailable", "unknown_user", "invalid_claims"])
async def test_selection_refuses_every_source_of_a_principal_the_send_gate_refuses(
    monkeypatch, refusal
) -> None:
    from sibyl.persistence.auth_common import InvalidAuthClaimsError, UserNotFoundError
    from sibyl_core.services.source_observations import SourceUnavailableError

    error = {
        "unavailable": SourceUnavailableError(),
        "unknown_user": UserNotFoundError("gone"),
        "invalid_claims": InvalidAuthClaimsError("gone"),
    }[refusal]
    send = AsyncMock(side_effect=error)
    selection = _selection(monkeypatch, send)
    sources = [
        _raw_memory(id=f"left-{index}", revision=1, observed_revision=1) for index in range(3)
    ]

    assert [await selection.fresh(source) for source in sources] == [False] * 3
    assert [await selection.neighbour(source) for source in sources] == [False] * 3
    assert selection.selected == {}
    assert selection.errors == {}
    send.assert_awaited_once_with(ORG_ID, USER_ID)


async def test_an_erroring_source_reflected_alone_still_counts_against_the_limit(
    monkeypatch,
) -> None:
    from sibyl.jobs import reflection
    from sibyl_core.services.memory_source_validation import SourceReadAuthority

    selection = _selection(
        monkeypatch, AsyncMock(return_value=SourceReadAuthority(USER_ID)), returning=1
    )
    sources = [
        _raw_memory(id=f"alone-{index}", revision=1, observed_revision=1) for index in range(2)
    ]
    selection.paged = frozenset((USER_ID, source.id, "inc", 1) for source in sources)
    selection.observed = {source.id: ("inc", 1) for source in sources}
    monkeypatch.setattr(reflection, "_load_dream_work", AsyncMock(side_effect=RuntimeError("down")))

    # The failure is still carried to the receipt, but the source takes a
    # returning place rather than slipping in as fresh.
    assert await selection.neighbour(sources[0]) is True
    assert await selection.neighbour(sources[1]) is False
    assert set(selection.errors) == {"alone-0"}


async def test_selection_counts_candidates_without_a_readable_observation(monkeypatch) -> None:
    from sibyl.jobs import reflection
    from sibyl_core.services.memory_source_validation import SourceReadAuthority

    selection = _selection(monkeypatch, AsyncMock(return_value=SourceReadAuthority(USER_ID)))
    read = AsyncMock(return_value={"seen": ("inc", 1)})
    monkeypatch.setattr(reflection, "current_source_observations", read)
    batch = [_raw_memory(id="seen"), _raw_memory(id="lost")]

    await selection.prefetch(batch)
    await selection.prefetch(batch)

    # The miss falls back to full authorization and is logged once, not reread.
    assert selection.unobserved == {"lost"}
    read.assert_awaited_once_with(ORG_ID, ["seen", "lost"])
