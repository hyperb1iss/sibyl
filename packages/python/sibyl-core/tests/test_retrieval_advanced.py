"""Tests for advanced retrieval modules: dedup.py and hybrid.py.

Covers:
- EntityDeduplicator: vectorized similarity, pair finding, merge suggestions
- Hybrid search: vector + graph fusion, RRF merge, temporal boosting
- Score normalization and result merging from multiple sources
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import numpy as np
import pytest

import sibyl_core.retrieval.hybrid as hybrid_module
import sibyl_core.retrieval.search as search_module
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.models.context import ContextFacet
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.retrieval.candidates import CandidateKind, RetrievalCandidate
from sibyl_core.retrieval.dedup import (
    DedupConfig,
    DuplicatePair,
    EntityDeduplicator,
    cosine_similarity,
    get_deduplicator,
    jaccard_similarity,
)
from sibyl_core.retrieval.fact_frames import (
    extract_evidence_fact_frames,
    extract_query_fact_frames,
    score_fact_frame_match,
)
from sibyl_core.retrieval.hybrid import (
    HybridConfig,
    HybridResult,
    graph_traversal,
    hybrid_search,
    simple_hybrid_search,
    vector_search,
)
from sibyl_core.retrieval.query_ranking import (
    QueryCoverageCandidate,
    QueryCoverageRankedCandidate,
    QueryCoverageResult,
    extract_keywords,
    rank_by_query_coverage,
    should_accept_query_coverage_refinement,
)
from sibyl_core.retrieval.reranking import RerankResult
from sibyl_core.retrieval.search import RetrievalPlan, RetrievalSignal, RetrievalWeights
from sibyl_core.retrieval.temporal import (
    get_entity_decay_timestamp,
    temporal_boost,
    temporal_decay_multiplier,
)
from sibyl_core.services.graph import EntityManager, SurrealGraphClient, prepare_graph_schema

# =============================================================================
# Test Fixtures and Mock Infrastructure
# =============================================================================


def test_query_coverage_keywords_drop_conversational_scaffolding() -> None:
    keywords = extract_keywords(
        "I was thinking about our previous conversation. "
        "Can you remind me what two-factor authentication methods you mentioned?"
    )

    assert keywords == ["two-factor", "authentication", "method"]
    assert "thi" not in keywords
    assert "previou" not in keywords


def test_query_coverage_keywords_keep_real_singulars() -> None:
    keywords = extract_keywords(
        "I'm checking this previous data privacy exercise used resources from companies."
    )

    assert keywords == ["data", "privacy", "exercise", "used", "resource", "company"]


def test_query_coverage_keywords_drop_answer_shape_scaffolding() -> None:
    keywords = extract_keywords("What type of rice is my favorite?")

    assert keywords == ["rice", "favorite"]


def test_temporal_boost_uses_citation_stamp_before_age_fallback() -> None:
    now = datetime(2026, 7, 3, tzinfo=UTC)
    cited = make_entity_for_test(
        "cited",
        created_at=now - timedelta(days=420),
        metadata={
            "last_used_at": (now - timedelta(days=2)).isoformat(),
            "citation_count": 1,
        },
    )
    uncited = make_entity_for_test(
        "uncited",
        created_at=now - timedelta(days=420),
        metadata={"retrieval_count": 0, "citation_count": 0},
    )

    boosted = temporal_boost(
        [(uncited, 0.95), (cited, 0.90)],
        decay_days=30,
        reference_time=now,
    )

    assert boosted[0][0].id == "cited"
    assert temporal_decay_multiplier(cited, decay_days=30, reference_time=now) > 0.9
    assert temporal_decay_multiplier(uncited, decay_days=30, reference_time=now) == 0.1


def test_temporal_decay_weights_exposure_below_citation() -> None:
    now = datetime(2026, 7, 3, tzinfo=UTC)
    cited = make_entity_for_test(
        "cited",
        created_at=now - timedelta(days=420),
        metadata={
            "last_used_at": (now - timedelta(days=1)).isoformat(),
            "citation_count": 1,
        },
    )
    exposed = make_entity_for_test(
        "exposed",
        created_at=now - timedelta(days=420),
        metadata={
            "last_recalled_at": (now - timedelta(days=1)).isoformat(),
            "retrieval_count": 10,
        },
    )
    uncited = make_entity_for_test("uncited", created_at=now - timedelta(days=420))

    cited_multiplier = temporal_decay_multiplier(cited, decay_days=30, reference_time=now)
    exposed_multiplier = temporal_decay_multiplier(exposed, decay_days=30, reference_time=now)

    assert cited_multiplier > 0.9
    assert 0.1 < exposed_multiplier < cited_multiplier
    assert temporal_decay_multiplier(uncited, decay_days=30, reference_time=now) == 0.1


def test_last_accessed_compatibility_does_not_outrank_citation() -> None:
    now = datetime(2026, 7, 3, tzinfo=UTC)
    cited = make_entity_for_test(
        "cited",
        created_at=now - timedelta(days=420),
        metadata={
            "last_accessed_at": (now - timedelta(days=1)).isoformat(),
            "last_used_at": (now - timedelta(days=240)).isoformat(),
            "citation_count": 1,
        },
    )
    accessed = make_entity_for_test(
        "accessed",
        created_at=now - timedelta(days=420),
        metadata={"last_accessed_at": (now - timedelta(days=1)).isoformat()},
    )

    assert get_entity_decay_timestamp(cited) == now - timedelta(days=240)
    assert get_entity_decay_timestamp(accessed) > now - timedelta(days=120)


def test_usage_decay_timestamp_never_precedes_validity_floor() -> None:
    now = datetime(2026, 7, 3, tzinfo=UTC)
    entity = make_entity_for_test(
        "valid-newer-than-citation",
        created_at=now - timedelta(days=420),
        metadata={
            "last_accessed_at": (now - timedelta(days=2)).isoformat(),
            "last_used_at": (now - timedelta(days=100)).isoformat(),
            "valid_from": (now - timedelta(days=10)).isoformat(),
        },
    )

    assert get_entity_decay_timestamp(entity) == now - timedelta(days=10)


def test_native_candidate_ranking_applies_usage_aware_decay() -> None:
    now = datetime(2026, 7, 3, tzinfo=UTC)

    def candidate(
        candidate_id: str,
        metadata: dict[str, Any],
    ) -> RetrievalCandidate:
        return RetrievalCandidate(
            id=candidate_id,
            type="note",
            name=candidate_id,
            content="usage aware forgetting fixture",
            score=0.8,
            source=candidate_id,
            metadata=metadata,
            kind=CandidateKind.NODE,
            created_at=now - timedelta(days=420),
        )

    uncited = candidate("uncited", {})
    cited = candidate(
        "cited",
        {
            "last_used_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "citation_count": 1,
        },
    )
    plan = RetrievalPlan(
        query="usage aware forgetting fixture",
        organization_id="org-123",
        facets=(ContextFacet.RECENT_MEMORY,),
        facet_types={ContextFacet.RECENT_MEMORY: ("note",)},
        scopes=(),
        denied_scopes=(),
        weights=RetrievalWeights(freshness_boost_cap=1.0),
    )

    ranked = search_module._rank_fused_candidates(
        [(RetrievalSignal.NODE_FULLTEXT, [uncited, cited])],
        plan=plan,
        limit=2,
        rrf_scores={"uncited": 0.8, "cited": 0.79},
    )

    assert ranked[0][0].id == "cited"
    assert ranked[0][2]["temporal_decay_multiplier"] > ranked[1][2]["temporal_decay_multiplier"]


def test_native_candidate_ranking_preserves_explicit_temporal_target() -> None:
    target = datetime(2025, 7, 3, tzinfo=UTC)
    old_target_match = RetrievalCandidate(
        id="old-target-match",
        type="note",
        name="old target match",
        content="temporal target fixture",
        score=0.8,
        source="old-target-match",
        metadata={"valid_at": target.isoformat()},
        kind=CandidateKind.NODE,
        created_at=target,
    )
    plan = RetrievalPlan(
        query="what happened last year in the temporal target fixture",
        organization_id="org-123",
        facets=(ContextFacet.RECENT_MEMORY,),
        facet_types={ContextFacet.RECENT_MEMORY: ("note",)},
        scopes=(),
        denied_scopes=(),
        weights=RetrievalWeights(freshness_boost_cap=1.0),
    )

    ranked = search_module._rank_fused_candidates(
        [(RetrievalSignal.NODE_FULLTEXT, [old_target_match])],
        plan=plan,
        limit=1,
        rrf_scores={"old-target-match": 0.8},
        temporal_target=target,
    )

    assert ranked[0][0].id == "old-target-match"
    assert "temporal_decay_multiplier" not in ranked[0][2]


def test_episode_record_candidates_keep_usage_metadata() -> None:
    row = {
        "uuid": "episode-1",
        "name": "Episode",
        "content": "episode content",
        "group_id": "org-123",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "last_recalled_at": datetime(2026, 7, 1, tzinfo=UTC),
        "last_used_at": datetime(2026, 6, 1, tzinfo=UTC),
        "retrieval_count": 4,
        "citation_count": 1,
    }

    candidate = search_module._candidate_from_episode_record(
        row,
        signal=RetrievalSignal.EPISODE_FULLTEXT,
        score=1.0,
    )

    assert candidate.metadata["last_recalled_at"] == row["last_recalled_at"]
    assert candidate.metadata["last_used_at"] == row["last_used_at"]
    assert candidate.metadata["retrieval_count"] == 4
    assert candidate.metadata["citation_count"] == 1


def test_query_coverage_keywords_drop_temporal_chatter() -> None:
    keywords = extract_keywords("How long will I be working in my current role tonight?")

    assert keywords == ["working", "role"]


def test_query_coverage_keywords_normalize_common_business_typo() -> None:
    keywords = extract_keywords(
        "What was the significant buisiness milestone I mentioned four weeks ago?"
    )

    assert keywords == ["business", "milestone"]


def test_query_coverage_keywords_strip_quoted_answer_terms() -> None:
    keywords = extract_keywords(
        "Which book did I finish reading first, 'The Hate U Give' or 'The Nightingale'?"
    )

    assert keywords == ["book", "finish", "reading", "first", "hate", "give", "nightingale"]


def test_query_coverage_promotes_favorite_fact_over_shape_words() -> None:
    ranked = _rank_query_ids(
        "What type of rice is my favorite?",
        [
            "User: I compared every type of pasta for dinner.",
            "User: I read about different type categories for grains.",
            "User: I need a type chart for cooking utensils.",
            "User: This recipe mentions rice as a side dish.",
            "User: I asked about rice cooker settings.",
            "User: My favorite rice is Japanese short grain rice.",
        ],
    )

    assert ranked[0] == "5"


def test_query_coverage_rescues_strong_preference_tail_candidate() -> None:
    ranked = _rank_query_ids(
        "Any tips for better phone battery life?",
        [
            "User: I saved a general app organization note.",
            "User: I compared laptop sleeves and desk accessories.",
            "User: I asked for travel tips about packing light.",
            "User: I organized photos on my phone.",
            "User: I updated calendar reminders.",
            "User: My phone battery lasts longer when I carry a portable power bank "
            "and wireless charging pad.",
        ],
    )

    assert "5" in ranked[:5]


def test_query_coverage_promotes_phone_accessory_evidence() -> None:
    ranked = _rank_query_ids(
        "Can you suggest some useful accessories for my phone?",
        [
            "User: I asked for advice about cat chew toys.",
            "User: I bought gaming accessories for a PS5.",
            "User: I asked for healthy lunch ideas.",
            "User: I wanted outfit inspiration for white sneakers.",
            "User: I compared smart light bulb brands.",
            "User: I need a new screen protector for my iPhone 13 Pro.",
        ],
    )

    assert "5" in ranked[:5]


def test_query_coverage_preserves_sparse_preference_top_five_candidate() -> None:
    ranked = _rank_query_ids(
        "Can you recommend a show or movie for me to watch tonight?",
        [
            "User: I asked for book recommendations about ancient civilizations tonight.",
            "User: I finished a true crime podcast tonight and wanted more episodes.",
            "User: As an aspiring stand-up comedian, I'm looking for advice about "
            "Netflix specials with strong storytelling.",
            "User: I saved train-themed board game recommendations.",
            "User: I wrote a travel note about Denver tonight.",
            "User: Here are generic movie ideas to watch tonight.",
            "User: I compared TV show release schedules tonight.",
            "User: I tracked movie theater listings tonight.",
        ],
    )

    assert "2" in ranked[:5]


def test_query_coverage_preserves_temporal_top_five_candidates() -> None:
    ranked = _rank_query_ids(
        "Which event happened first, fixing the fence or trimming the goats' hooves?",
        [
            "User: I fixed the fence before the storm.",
            "User: I logged a farm supply receipt.",
            "User: I wrote down a pasture maintenance note.",
            "User: I planned chores around the open house.",
            "User: I trimmed the goats' hooves two weeks ago.",
            "User: Which event happened first is hard to infer from unrelated notes.",
        ],
    )

    assert "4" in ranked[:5]
    assert "5" not in ranked[:5]


def test_query_coverage_uses_temporal_target_with_concept_evidence() -> None:
    target = datetime(2026, 1, 10, tzinfo=UTC)
    result = rank_by_query_coverage(
        "What kitchen appliance did I buy 10 days ago?",
        [
            QueryCoverageCandidate(
                item="phone",
                stable_id="phone",
                text="User: My Samsung phone battery has been unreliable.",
                prior_score=1.0,
                original_rank=1,
                timestamp="2026/01/10 09:00",
            ),
            QueryCoverageCandidate(
                item="workshop",
                stable_id="workshop",
                text="User: I organized a sustainable living workshop.",
                prior_score=0.99,
                original_rank=2,
                timestamp="2026/01/10 12:00",
            ),
            QueryCoverageCandidate(
                item="shoes",
                stable_id="shoes",
                text="User: I bought running shoes for a picnic.",
                prior_score=0.98,
                original_rank=3,
                timestamp="2026/01/10 14:00",
            ),
            QueryCoverageCandidate(
                item="sushi",
                stable_id="sushi",
                text="User: I learned about uramaki sushi rolls.",
                prior_score=0.97,
                original_rank=4,
                timestamp="2026/01/10 16:00",
            ),
            QueryCoverageCandidate(
                item="closet",
                stable_id="closet",
                text="User: I organized my closet and made a shoe list.",
                prior_score=0.96,
                original_rank=5,
                timestamp="2026/01/10 18:00",
            ),
            QueryCoverageCandidate(
                item="smoker",
                stable_id="smoker",
                text="User: I got a smoker today and want BBQ sauce recipes.",
                prior_score=0.95,
                original_rank=6,
                timestamp="2026/01/10 11:00",
            ),
        ],
        temporal_target=target,
    )

    assert "smoker" in [candidate.stable_id for candidate in result.ranked[:5]]


def test_query_coverage_promotes_brand_lookup_evidence() -> None:
    ranked = _rank_query_ids(
        "What brand of shampoo do I currently use?",
        [
            "User: I bought skincare products from Sephora for dry skin.",
            "User: I washed running socks after a workout.",
            "User: I organized my bathroom cleaning schedule.",
            "User: I compared hair dryers and towels.",
            "User: I asked for generic hair care tips.",
            "User: I've been using a lavender scented shampoo that I picked up at Trader Joe's.",
        ],
    )

    assert "5" in ranked[:5]


def test_query_coverage_promotes_sibling_count_evidence_set() -> None:
    ranked = _rank_query_ids(
        "What is the total number of siblings I have?",
        [
            "User: I read demographic tables about age groups.",
            "User: I asked about area calculations for a circular shield.",
            "User: I researched professional network gender dynamics.",
            "User: I come from a family with 3 sisters.",
            "User: I have a brother who influences my social circle.",
            "User: I compared book club participation by age.",
        ],
    )

    assert {"3", "4"} <= set(ranked[:5])


def test_query_coverage_promotes_age_arithmetic_evidence_set() -> None:
    ranked = _rank_query_ids(
        "How many years older am I than when I graduated from college?",
        [
            "User: I planned a graduation ceremony for a colleague's daughter.",
            "User: I asked about online marketing courses.",
            "User: I have a Bachelor's degree that I completed at the age of 25.",
            "User: As a 32-year-old Digital Marketing Specialist, I want to advance.",
            "User: I saved notes about certification providers.",
            "User: I organized old photos from my grandma's album.",
        ],
    )

    assert {"2", "3"} <= set(ranked[:5])


def test_query_coverage_promotes_personal_sports_event_with_temporal_target() -> None:
    target = datetime(2023, 6, 17, tzinfo=UTC)
    result = rank_by_query_coverage(
        "I mentioned participating in a sports event two weeks ago. What was the event?",
        [
            QueryCoverageCandidate(
                item="social",
                stable_id="social",
                text="User: I organized a social event with games.",
                prior_score=1.0,
                original_rank=1,
                timestamp="2023/06/17 20:20",
            ),
            QueryCoverageCandidate(
                item="beach",
                stable_id="beach",
                text="User: I planned a family beach trip.",
                prior_score=0.99,
                original_rank=2,
                timestamp="2023/06/17 11:56",
            ),
            QueryCoverageCandidate(
                item="food",
                stable_id="food",
                text="User: I planned a food festival booth.",
                prior_score=0.98,
                original_rank=3,
                timestamp="2023/06/17 18:00",
            ),
            QueryCoverageCandidate(
                item="coins",
                stable_id="coins",
                text="User: I researched a coin collecting event.",
                prior_score=0.97,
                original_rank=4,
                timestamp="2023/06/17 21:40",
            ),
            QueryCoverageCandidate(
                item="shoes",
                stable_id="shoes",
                text="User: I compared running shoe cushioning.",
                prior_score=0.96,
                original_rank=5,
                timestamp="2023/06/17 10:00",
            ),
            QueryCoverageCandidate(
                item="soccer",
                stable_id="soccer",
                text="User: I will participate in the company charity soccer tournament today.",
                prior_score=0.92,
                original_rank=6,
                timestamp="2023/06/17 15:20",
            ),
        ],
        temporal_target=target,
    )

    assert "soccer" in [candidate.stable_id for candidate in result.ranked[:5]]


def test_query_coverage_promotes_related_temporal_cluster_support() -> None:
    target = datetime(2023, 2, 1, tzinfo=UTC)
    result = rank_by_query_coverage(
        "What did I do with Rachel on the Wednesday two months ago?",
        [
            QueryCoverageCandidate(
                item="ukulele-anchor",
                stable_id="ukulele-anchor",
                text=(
                    "User: I started taking ukulele lessons with my friend Rachel "
                    "today and practiced chord changes on my Yamaha keyboard."
                ),
                prior_score=1.0,
                original_rank=1,
                timestamp="2023/02/01 14:00",
            ),
            QueryCoverageCandidate(
                item="rachel-distractor",
                stable_id="rachel-distractor",
                text="User: Rachel sent me a recipe for dinner.",
                prior_score=0.99,
                original_rank=2,
                timestamp="2023/02/01 15:00",
            ),
            QueryCoverageCandidate(
                item="generic-a",
                stable_id="generic-a",
                text="User: I researched League Cup formats.",
                prior_score=0.98,
                original_rank=3,
                timestamp="2023/02/01 16:00",
            ),
            QueryCoverageCandidate(
                item="generic-b",
                stable_id="generic-b",
                text="User: I planned a weekend museum visit.",
                prior_score=0.97,
                original_rank=4,
                timestamp="2023/02/01 17:00",
            ),
            QueryCoverageCandidate(
                item="generic-c",
                stable_id="generic-c",
                text="User: I checked train schedules.",
                prior_score=0.96,
                original_rank=5,
                timestamp="2023/02/01 18:00",
            ),
            QueryCoverageCandidate(
                item="ukulele-support",
                stable_id="ukulele-support",
                text=(
                    "User: My Yamaha keyboard practice helped my ukulele chord "
                    "changes, and I asked for fingerpicking drills."
                ),
                prior_score=0.75,
                original_rank=6,
                timestamp="2023/02/01 19:00",
            ),
        ],
        temporal_target=target,
    )

    assert "ukulele-support" in [candidate.stable_id for candidate in result.ranked[:5]]


def test_query_coverage_penalizes_assistant_only_memory_matches() -> None:
    ranked = _rank_query_ids(
        "How many years older is my grandma than me?",
        [
            (
                "User: Can you explain how to calculate age differences in family "
                "trees? Assistant: Compare your age with your grandma's age."
            ),
            "User: I made a birthday card.",
            "User: I organized old photos.",
            "User: I am 32 years old and tracking family milestones.",
            "User: My grandma is 78 years old and still loves gardening.",
            "User: Assistant, explain older relatives in genealogy charts.",
        ],
    )

    assert {"3", "4"} <= set(ranked[:5])
    assert ranked.index("4") < ranked.index("5")


def test_query_coverage_treats_generic_events_as_weak_sports_evidence() -> None:
    result = rank_by_query_coverage(
        "What is the order of the three sports events I participated in?",
        [
            QueryCoverageCandidate(
                item="generic",
                stable_id="generic",
                text="User: Can you suggest family-friendly events for kids to attend?",
                prior_score=1.0,
                original_rank=1,
            ),
            QueryCoverageCandidate(
                item="run",
                stable_id="run",
                text="User: I just finished a 5K run with a personal best time.",
                prior_score=0.99,
                original_rank=2,
            ),
        ],
    )
    overlap_by_id = {candidate.stable_id: candidate.overlap for candidate in result.ranked}

    assert overlap_by_id["generic"] < overlap_by_id["run"]


def test_query_coverage_promotes_business_milestone_evidence() -> None:
    ranked = _rank_query_ids(
        "What was the significant business milestone I mentioned four weeks ago?",
        [
            "User: I organized my task list and project notes.",
            "User: I read articles about European politics.",
            "User: I launched my website and created a business plan outline.",
            "User: I asked about indoor plants.",
            "User: I signed a contract with my first freelance client today.",
        ],
    )

    assert {"2", "4"} <= set(ranked[:5])


def test_query_coverage_promotes_business_milestone_with_common_typo() -> None:
    ranked = _rank_query_ids(
        "What was the significant buisiness milestone I mentioned four weeks ago?",
        [
            "User: My supervisor Rachel helped me settle into my new role.",
            "User: I read articles about European politics.",
            "User: I just launched my website and created a business plan outline.",
            "User: Which industries in Ibadan are growing fastest?",
            "User: I just signed a contract with my first freelance client today.",
        ],
    )

    assert {"2", "4"} <= set(ranked[:5])


def test_query_coverage_keeps_specific_social_activity_evidence() -> None:
    ranked = _rank_query_ids(
        "Which event happened first, my participation in the #PlankChallenge "
        "or my post about vegan chili recipe?",
        [
            "User: I posted photos from a racing event on Instagram.",
            "User: I shared a vegan chili recipe using #FoodieAdventures.",
            "User: I participated in a social media challenge called #PlankChallenge.",
            "User: I asked for sourdough recipe headings.",
            "User: I planned vegetarian protein meals.",
            "User: I watched a new TV show.",
        ],
    )

    assert {"1", "2"} <= set(ranked[:5])


def test_query_coverage_promotes_assistant_created_artifact_sections() -> None:
    ranked = _rank_query_ids(
        "Can you remind me what the chord progression was for the chorus "
        "in the second sad song you created?",
        [
            "User: I asked about guitar practice. Assistant: Here are some music "
            "theory resources about chord progressions.",
            "User: I asked about classic rock lyrics. Assistant: Stairway to Heaven "
            "has famous lyrics and a long structure.",
            "User: I asked about jewelry cleaning. Assistant: Here are some "
            "cleaning tips for a bracelet.",
            "User: I asked for piano exercises. Assistant: Practice common chord "
            "progressions slowly.",
            "User: Create two sad songs with notes. Assistant: Here's a sad song "
            "with notes for you. Verse 1: C D E E E D C C. Chorus: G G G G A "
            "G F. Here's another sad song. Verse 1: A B C C. Chorus: Am F C G.",
        ],
    )

    assert ranked[0] == "4"


def test_query_coverage_completes_evidence_sets_over_low_signal_distractors() -> None:
    ranked = _rank_query_ids(
        "How many different doctors did I visit?",
        [
            "User: I organized a weekly calendar for household chores.",
            "User: I watched a science fiction show with a doctor character.",
            "User: I compared health insurance websites.",
            "User: I visited Dr. Smith for a therapy appointment.",
            "User: I read an article about hospital architecture.",
            "User: I saw my dermatologist for a skin check.",
            "User: I went to the eye doctor for a new prescription.",
        ],
    )

    assert {"3", "5", "6"} <= set(ranked[:5])


def test_query_coverage_keeps_strong_aggregate_evidence_window_stable() -> None:
    ranked = _rank_query_ids(
        "How many incidents did I document across project notes?",
        [
            "User: I documented one production incident in the project journal.",
            "User: I documented a second project incident after the deploy.",
            "User: I added notes about a billing incident in the project log.",
            "User: I documented the support incident in project notes.",
            "User: I wrote project notes after another incident review.",
            "User: I documented project notes about an incident after lunch.",
        ],
    )

    assert set(ranked[:5]) == {"0", "1", "2", "3", "4"}
    assert "5" not in ranked[:5]


def test_query_coverage_promotes_event_action_evidence() -> None:
    ranked = _rank_query_ids(
        "How many different events did I volunteer at, present at, or attend?",
        [
            "User: Can you suggest family-friendly places for kids?",
            "User: I asked about stretching before long walks.",
            "User: I planned a dinner menu after a long commute.",
            "User: I compared calendars for next month.",
            "User: I watched a documentary about planning conferences.",
            "User: I volunteered at the museum opening and helped with the gallery tour.",
            "User: I presented a small print series at a community art exhibition.",
            "User: I attended a lecture by a textile artist at the downtown gallery.",
        ],
    )

    assert "5" in ranked[:5]


def test_query_coverage_promotes_service_action_evidence() -> None:
    ranked = _rank_query_ids(
        "How many different services did I order, rely on, or subscribe to?",
        [
            "User: I cooked dinner with basil from the garden.",
            "User: I compared grocery budgets for the week.",
            "User: I asked for restaurant recommendations downtown.",
            "User: I made a meal plan for Sunday.",
            "User: I saved a coupon for kitchen storage.",
            "User: I ordered pizza delivery after work.",
            "User: I've been relying on Uber Eats when meetings run late.",
            "User: I subscribed to a weekly meal service for busy nights.",
        ],
    )

    assert "5" in ranked[:5]


def test_query_coverage_promotes_reliance_evidence() -> None:
    ranked = _rank_query_ids(
        "Which service have I been relying on when meetings run late?",
        [
            "User: I cooked dinner with basil from the garden.",
            "User: I compared grocery budgets for the week.",
            "User: I asked for restaurant recommendations downtown.",
            "User: I made a meal plan for Sunday.",
            "User: I saved a coupon for kitchen storage.",
            "User: I've been relying on Uber Eats when meetings run late.",
        ],
    )

    assert ranked[0] == "5"


def test_query_coverage_promotes_object_action_evidence() -> None:
    ranked = _rank_query_ids(
        "How many things did I buy, assemble, sell, or fix?",
        [
            "User: I bought new screws for a kitchen drawer.",
            "User: I read a moving checklist for renters.",
            "User: I asked about cleaning fabric stains.",
            "User: I compared home office lighting.",
            "User: I saved an article about interior design.",
            "User: I assembled the bookshelf for my living room.",
            "User: I sold my old couch before rearranging the apartment.",
            "User: I fixed the wobbly coffee table after dinner.",
        ],
    )

    assert {"5", "6"} <= set(ranked[:5])


def test_query_coverage_promotes_repair_action_evidence() -> None:
    ranked = _rank_query_ids(
        "Which item did I fix after dinner?",
        [
            "User: I bought new screws for a kitchen drawer.",
            "User: I read a moving checklist for renters.",
            "User: I asked about cleaning fabric stains.",
            "User: I compared home office lighting.",
            "User: I saved an article about interior design.",
            "User: I assembled the bookshelf for my living room.",
            "User: I sold my old couch before rearranging the apartment.",
            "User: I fixed the wobbly coffee table after dinner.",
        ],
    )

    assert "7" in ranked[:5]


def test_query_coverage_promotes_recurring_frequency() -> None:
    ranked = _rank_query_ids(
        "How often do I attend classes to help with my anxiety?",
        [
            "User: I asked about anxiety breathing exercises.",
            "User: I read about class scheduling software.",
            "User: I planned weekend errands around the gym.",
            "User: I compared meditation apps.",
            "User: I saved an article about sleep routines.",
            "User: I attend classes twice a week to help with anxiety.",
        ],
    )

    assert "5" in ranked[:5]


def test_fact_frames_extract_generic_service_usage() -> None:
    query_frames = extract_query_fact_frames("What audio app have I been using lately?")
    evidence_frames = extract_evidence_fact_frames(
        "User: I've been listening to history podcasts through Pocket Casts lately."
    )

    assert any("service" in frame.categories for frame in query_frames)
    assert any({"service", "media"} <= frame.categories for frame in evidence_frames)
    assert (
        score_fact_frame_match(
            "What audio app have I been using lately?",
            "User: I've been listening to history podcasts through Pocket Casts lately.",
        )
        >= 0.8
    )


def test_fact_frames_extract_media_platform_usage_from_on_phrase() -> None:
    evidence_frames = extract_evidence_fact_frames(
        "User: I've been listening to their songs a lot on Spotify lately."
    )

    assert any({"service", "media"} <= frame.categories for frame in evidence_frames)
    assert (
        score_fact_frame_match(
            "What is the name of the music streaming service have I been using lately?",
            "User: I've been listening to their songs a lot on Spotify lately.",
        )
        >= 0.8
    )


def test_fact_frames_do_not_treat_plain_service_as_repair_action() -> None:
    assert (
        score_fact_frame_match(
            "What music streaming service have I been using lately?",
            "Assistant: Music collectors often compare rare records. "
            "Vinyl Me, Please is a popular online vinyl subscription service "
            "that offers appraisal services.",
        )
        < 0.8
    )


def test_fact_frames_ignore_calendar_prepositions_as_services() -> None:
    frames = extract_query_fact_frames("What time did I reach the clinic on Monday?")

    assert not frames


def test_query_coverage_uses_fact_frames_for_service_usage() -> None:
    ranked = _rank_query_ids(
        "What audio app have I been using lately?",
        [
            "User: I compared Bluetooth speakers for my desk.",
            "User: I read a forum thread about phone app permissions.",
            "User: I asked for podcast microphone recommendations.",
            "User: I updated a playlist for a road trip.",
            "User: I organized my notes about local concerts.",
            "User: I've been listening to history podcasts through Pocket Casts lately.",
        ],
    )

    assert "5" in ranked[:5]


def test_query_coverage_uses_fact_frames_for_media_platform_usage() -> None:
    ranked = _rank_query_ids(
        "What is the name of the music streaming service have I been using lately?",
        [
            "User: I asked for music theory resources.",
            "User: I compared airline services after a long flight.",
            "User: I'm looking for local concert recommendations.",
            "User: I updated permissions for a phone app.",
            "User: I read about streaming equipment for microphones.",
            "User: I've been listening to their songs a lot on Spotify lately.",
        ],
    )

    assert "5" in ranked[:5]


def test_query_coverage_blends_fact_frames_into_recommendation_ranking() -> None:
    ranked = _rank_query_ids(
        "I've got some free time tonight, any documentary recommendations?",
        [
            "User: I'm looking for book recommendations this weekend.",
            "User: Can you recommend gaming accessories for my console?",
            "User: I asked for restaurant recommendations for a trip.",
            "User: I want a board game recommendation for game night.",
            "User: Can you recommend a cafe near my apartment?",
            "User: I've been watching a lot of documentaries lately, especially on Netflix.",
        ],
    )

    assert "5" in ranked[:5]


def test_query_coverage_uses_fact_frames_for_relative_life_event() -> None:
    ranked = _rank_query_ids(
        "Which life event for a relative did I attend?",
        [
            "User: I compared train routes for a spring trip.",
            "User: I helped a friend choose a birthday gift.",
            "User: I read about family history archives.",
            "User: I planned a work dinner downtown.",
            "User: I watched a documentary about graduation traditions.",
            "User: I came back from my aunt's graduation ceremony.",
        ],
    )

    assert "5" in ranked[:5]


def test_query_coverage_uses_fact_frames_for_profile_recommendations() -> None:
    ranked = _rank_query_ids(
        "Can you recommend publications or conferences I might find interesting?",
        [
            "User: I asked for science fiction reading recommendations.",
            "User: I saved a generic essay about higher education.",
            "User: I compared social media workshop formats.",
            "User: I drafted a resume for a retail position.",
            "User: I bookmarked a public lecture series.",
            "User: I am working in computational biology and protein modeling.",
        ],
    )

    assert "5" in ranked[:5]


def test_query_coverage_uses_fact_frames_for_acquired_object_category() -> None:
    ranked = _rank_query_ids(
        "Which workshop tool did I buy?",
        [
            "User: I organized old hardware receipts.",
            "User: I bought coffee beans before the workshop.",
            "User: I asked for home office lighting ideas.",
            "User: I compared maker-space class schedules.",
            "User: I cleaned the garage after dinner.",
            "User: I picked up a cordless drill for the workshop.",
        ],
    )

    assert "5" in ranked[:5]


def test_query_coverage_keeps_temporal_no_target_on_conservative_path() -> None:
    ranked = _rank_query_ids(
        "Which workshop tool did I buy last week?",
        [
            "User: I organized old hardware receipts last week.",
            "User: I bought coffee beans before the workshop.",
            "User: I asked for home office lighting ideas.",
            "User: I compared maker-space class schedules.",
            "User: I cleaned the garage after dinner.",
            "User: I picked up a cordless drill for the workshop last week.",
        ],
    )

    assert "5" not in ranked[:5]


def test_query_coverage_keeps_multi_evidence_queries_on_existing_path() -> None:
    ranked = _rank_query_ids(
        "Which two workshop tools did I buy?",
        [
            "User: I organized old hardware receipts.",
            "User: I bought coffee beans before the workshop.",
            "User: I asked for home office lighting ideas.",
            "User: I compared maker-space class schedules.",
            "User: I cleaned the garage after dinner.",
            "User: I scheduled a workshop kickoff.",
            "User: I reviewed safety rules for the maker space.",
            "User: I picked up a cordless drill for the workshop.",
        ],
    )

    assert "7" not in ranked[:5]


def test_query_coverage_keeps_temporal_profile_requests_on_existing_path() -> None:
    ranked = _rank_query_ids(
        "Recommend, from earliest to latest, publications I might enjoy",
        [
            "User: I asked for science fiction reading recommendations.",
            "User: I saved a generic essay about higher education.",
            "User: I compared social media workshop formats.",
            "User: I drafted a resume for a retail position.",
            "User: I bookmarked a public lecture series.",
            "User: I am working in computational biology and protein modeling.",
        ],
    )

    assert "5" not in ranked[:5]


def test_query_coverage_refinement_accepts_top_window_signal_gain() -> None:
    initial = _coverage_result(
        [
            ("answer-current", 1.0),
            ("related", 0.67),
            ("specific-distractor", 1.0),
            ("personal-evidence", 1.0),
            ("weak-top", 0.67),
            ("answer-tail", 1.0),
            ("tail-a", 0.33),
            ("tail-b", 0.33),
            ("tail-c", 1.0),
            ("tail-d", 0.33),
        ],
    )
    refined = _coverage_result(
        [
            ("answer-current", 1.0),
            ("related", 0.67),
            ("specific-distractor", 1.0),
            ("personal-evidence", 1.0),
            ("answer-tail", 1.0),
            ("weak-top", 0.67),
            ("tail-a", 0.33),
            ("tail-b", 0.33),
            ("tail-c", 1.0),
            ("tail-d", 0.33),
        ],
        changed=True,
    )

    assert should_accept_query_coverage_refinement(initial, refined) is True


def test_query_coverage_refinement_accepts_saturated_top_score_gain() -> None:
    initial = _coverage_result(
        [
            ("generic-top", 1.0),
            ("specific-answer", 1.0),
            ("related", 1.0),
            ("weak-a", 0.67),
            ("weak-b", 0.67),
        ],
        scores={
            "generic-top": 2.10,
            "specific-answer": 2.18,
            "related": 1.95,
            "weak-a": 1.2,
            "weak-b": 1.1,
        },
    )
    refined = _coverage_result(
        [
            ("specific-answer", 1.0),
            ("generic-top", 1.0),
            ("related", 1.0),
            ("weak-a", 0.67),
            ("weak-b", 0.67),
        ],
        changed=True,
        scores={
            "specific-answer": 2.18,
            "generic-top": 2.10,
            "related": 1.95,
            "weak-a": 1.2,
            "weak-b": 1.1,
        },
    )

    assert should_accept_query_coverage_refinement(initial, refined) is True


def test_query_coverage_refinement_rejects_top_ten_signal_loss() -> None:
    initial = _coverage_result(
        [
            ("answer-current", 1.0),
            ("related", 0.67),
            ("specific-distractor", 1.0),
            ("personal-evidence", 1.0),
            ("weak-top", 0.67),
            ("answer-tail", 1.0),
            ("tail-a", 0.33),
            ("tail-b", 0.33),
            ("tail-c", 1.0),
            ("tail-d", 1.0),
        ],
    )
    refined = _coverage_result(
        [
            ("answer-current", 1.0),
            ("related", 0.67),
            ("specific-distractor", 1.0),
            ("personal-evidence", 1.0),
            ("answer-tail", 1.0),
            ("weak-top", 0.67),
            ("tail-a", 0.33),
            ("tail-b", 0.33),
            ("tail-c", 1.0),
            ("low-signal", 0.0),
        ],
        changed=True,
    )

    assert should_accept_query_coverage_refinement(initial, refined) is False


def _rank_query_ids(query: str, texts: list[str]) -> list[str]:
    result = rank_by_query_coverage(
        query,
        [
            QueryCoverageCandidate(
                item=str(index),
                stable_id=str(index),
                text=text,
                prior_score=1.0 - (index * 0.01),
                original_rank=index + 1,
            )
            for index, text in enumerate(texts)
        ],
    )
    return [ranked.stable_id for ranked in result.ranked]


def _coverage_result(
    rows: list[tuple[str, float]],
    *,
    changed: bool = False,
    scores: dict[str, float] | None = None,
) -> QueryCoverageResult[str]:
    return QueryCoverageResult(
        ranked=[
            QueryCoverageRankedCandidate(
                item=stable_id,
                stable_id=stable_id,
                score=(scores or {}).get(stable_id, 1.0 - (index * 0.01)),
                original_rank=index + 1,
                overlap=overlap,
            )
            for index, (stable_id, overlap) in enumerate(rows)
        ],
        applied=True,
        changed=changed,
    )


@dataclass
class MockGraphClientForDedup:
    """Mock GraphClient for deduplication tests.

    Simulates FalkorDB client with controllable entity embeddings
    for testing vectorized similarity operations.
    """

    entities_with_embeddings: list[tuple[str, str, str, list[float]]] = field(default_factory=list)
    redirect_count: int = 0
    query_history: list[str] = field(default_factory=list)
    read_calls: list[str] = field(default_factory=list)
    read_org_calls: list[tuple[str, str]] = field(default_factory=list)
    write_org_calls: list[tuple[str, str]] = field(default_factory=list)

    class MockDriver:
        """Mock driver for execute_query."""

        def __init__(self, parent: MockGraphClientForDedup):
            self.parent = parent

        async def execute_query(self, query: str, **params: Any) -> list[Any]:
            """Execute mock query and return configured results."""
            self.parent.query_history.append(query)

            # Handle embedding fetch
            if "name_embedding IS NOT NULL" in query:
                return self.parent.entities_with_embeddings

            # Handle relationship redirect - return count
            if "DELETE r" in query:
                self.parent.redirect_count += 1
                return [{"redirected": 1}]

            return []

    @property
    def client(self) -> MagicMock:
        """Return mock client with driver."""
        mock = MagicMock()
        mock.driver = self.MockDriver(self)
        return mock

    async def execute_read(self, query: str, **params: Any) -> list[Any]:
        """Execute an unscoped read."""
        self.read_calls.append(query)
        return await self.MockDriver(self).execute_query(query, **params)

    async def execute_read_org(self, query: str, organization_id: str, **params: Any) -> list[Any]:
        """Execute an org-scoped read."""
        self.read_org_calls.append((organization_id, query))
        return await self.MockDriver(self).execute_query(query, **params)

    async def execute_write(self, query: str, **params: Any) -> list[Any]:
        """Execute an unscoped write."""
        return await self.MockDriver(self).execute_query(query, **params)

    async def execute_write_org(self, query: str, organization_id: str, **params: Any) -> list[Any]:
        """Execute an org-scoped write."""
        self.write_org_calls.append((organization_id, query))
        return await self.MockDriver(self).execute_query(query, **params)


@dataclass
class HnswDedupClient:
    raw_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def execute_query(self, query: str, **params: Any) -> list[dict[str, Any]]:
        raise AssertionError("batched HNSW candidate lookup should use raw queries")

    async def execute_query_raw(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.raw_calls.append((query, params))
        return [
            {
                "status": "OK",
                "result": [
                    {
                        "seed_id": params["seed_id_0"],
                        "uuid": "existing_alpha",
                        "name": "Alpha",
                        "entity_type": "topic",
                        "score": 0.99,
                    }
                ],
            },
            {
                "status": "OK",
                "result": [
                    {
                        "seed_id": params["seed_id_1"],
                        "uuid": "existing_beta",
                        "name": "Beta",
                        "entity_type": "topic",
                        "score": 0.97,
                    }
                ],
            },
        ]


@dataclass
class HnswFallbackDedupClient:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def execute_query(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        seed_id = params["seed_id"]
        suffix = str(seed_id).removeprefix("incoming_")
        return [
            {
                "status": "OK",
                "result": [
                    {
                        "uuid": f"existing_{suffix}",
                        "name": str(suffix).title(),
                        "entity_type": "topic",
                        "score": 0.99,
                    }
                ],
            },
        ]


@dataclass
class MockEntityManagerForDedup:
    """Mock EntityManager for deduplication merge tests."""

    entities: dict[str, Entity] = field(default_factory=dict)
    deleted_ids: list[str] = field(default_factory=list)
    updated_ids: list[str] = field(default_factory=list)
    list_all_calls: list[dict[str, Any]] = field(default_factory=list)
    _group_id: str = "org-123"

    async def get(self, entity_id: str) -> Entity | None:
        """Get entity by ID."""
        return self.entities.get(entity_id)

    async def update(self, entity_id: str, updates: dict[str, Any]) -> Entity:
        """Update entity."""
        self.updated_ids.append(entity_id)
        entity = self.entities[entity_id]
        if "metadata" in updates:
            entity.metadata = updates["metadata"]
        return entity

    async def delete(self, entity_id: str) -> bool:
        """Delete entity."""
        if entity_id in self.entities:
            del self.entities[entity_id]
            self.deleted_ids.append(entity_id)
            return True
        return False

    async def list_all(
        self,
        limit: int = 1000,
        offset: int = 0,
        *,
        include_archived: bool = False,
    ) -> list[Entity]:
        """List entities with pagination for seam-driven dedup."""
        self.list_all_calls.append(
            {
                "limit": limit,
                "offset": offset,
                "include_archived": include_archived,
            }
        )
        del include_archived
        return list(self.entities.values())[offset : offset + limit]


@dataclass
class MockEntityManagerForHybrid:
    """Mock EntityManager for hybrid search tests."""

    search_results: list[tuple[Entity, float]] = field(default_factory=list)
    search_calls: list[dict[str, Any]] = field(default_factory=list)
    _group_id: str = "org-123"

    async def search(
        self,
        query: str,
        entity_types: list[EntityType] | None = None,
        limit: int = 10,
    ) -> list[tuple[Entity, float]]:
        """Return preconfigured search results."""
        self.search_calls.append({"query": query, "entity_types": entity_types, "limit": limit})
        results = self.search_results
        if entity_types:
            results = [(e, s) for e, s in results if e.entity_type in entity_types]
        return results[:limit]


@dataclass
class MockGraphClientForHybrid:
    """Mock GraphClient for hybrid search graph traversal tests."""

    traversal_results: list[dict[str, Any]] = field(default_factory=list)
    query_history: list[str] = field(default_factory=list)
    read_calls: list[str] = field(default_factory=list)
    read_org_calls: list[tuple[str, str]] = field(default_factory=list)

    class MockDriver:
        """Mock driver for execute_query."""

        def __init__(self, parent: MockGraphClientForHybrid):
            self.parent = parent

        async def execute_query(self, query: str, **params: Any) -> list[Any]:
            """Execute mock query and return configured results."""
            self.parent.query_history.append(query)
            return self.parent.traversal_results

    @property
    def client(self) -> MagicMock:
        """Return mock client with driver."""
        mock = MagicMock()
        mock.driver = self.MockDriver(self)
        return mock

    async def execute_read(self, query: str, **params: Any) -> list[dict[str, Any]]:
        """Execute an unscoped read."""
        self.read_calls.append(query)
        self.query_history.append(query)
        return self.traversal_results

    async def execute_read_org(
        self, query: str, organization_id: str, **params: Any
    ) -> list[dict[str, Any]]:
        """Execute an org-scoped read."""
        self.read_org_calls.append((organization_id, query))
        self.query_history.append(query)
        return self.traversal_results

    @staticmethod
    def normalize_result(result: Any) -> list[dict[str, Any]]:
        """Normalize query results."""
        if isinstance(result, list):
            return result
        return []


def make_entity_for_test(
    entity_id: str,
    name: str = "Test Entity",
    entity_type: EntityType = EntityType.TOPIC,
    description: str = "",
    created_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> Entity:
    """Factory for test entities."""
    return Entity(
        id=entity_id,
        name=name,
        entity_type=entity_type,
        description=description,
        content="",
        metadata=metadata or {},
        created_at=created_at or datetime.now(UTC),
    )


def make_graph_client(group_id: str = "org-123") -> SurrealGraphClient:
    return SurrealGraphClient(group_id=group_id, url="memory://")


# =============================================================================
# EntityDeduplicator Tests - Vectorized Similarity
# =============================================================================


class TestEntityDeduplicatorVectorized:
    """Test vectorized similarity operations in EntityDeduplicator."""

    def test_find_similar_pairs_empty_list(self) -> None:
        """Empty entity list returns no pairs."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        pairs = dedup._find_similar_pairs_vectorized([], threshold=0.9)
        assert pairs == []

    def test_find_similar_pairs_single_entity(self) -> None:
        """Single entity returns no pairs."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        entities = [("id1", "Entity One", "topic", [1.0, 0.0, 0.0])]
        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.9)
        assert pairs == []

    def test_find_similar_pairs_identical_embeddings(self) -> None:
        """Identical embeddings produce similarity 1.0."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.95,
            same_type_only=True,
            min_name_overlap=0.0,  # Disable name filter
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        embedding = [1.0, 0.5, 0.25, 0.125]
        entities = [
            ("id1", "Entity One", "topic", embedding),
            ("id2", "Entity Two", "topic", embedding),
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.95)
        assert len(pairs) == 1
        assert pairs[0].entity1_id == "id1"
        assert pairs[0].entity2_id == "id2"
        assert pairs[0].similarity == pytest.approx(1.0, rel=0.001)

    def test_find_similar_pairs_orthogonal_embeddings(self) -> None:
        """Orthogonal embeddings are not considered duplicates."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        entities = [
            ("id1", "Entity One", "topic", [1.0, 0.0, 0.0]),
            ("id2", "Entity Two", "topic", [0.0, 1.0, 0.0]),
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.5)
        assert pairs == []

    def test_find_similar_pairs_high_similarity(self) -> None:
        """Similar but not identical embeddings found above threshold."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.9,
            same_type_only=True,
            min_name_overlap=0.0,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        # Very similar embeddings
        entities = [
            ("id1", "Python programming", "topic", [1.0, 0.5, 0.3]),
            ("id2", "Python coding", "topic", [1.0, 0.51, 0.31]),  # Slightly different
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.9)
        assert len(pairs) == 1
        assert pairs[0].similarity > 0.9

    def test_find_similar_pairs_different_types_filtered(self) -> None:
        """Entities of different types are filtered when same_type_only=True."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.9,
            same_type_only=True,
            min_name_overlap=0.0,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        embedding = [1.0, 0.5, 0.3]
        entities = [
            ("id1", "Python", "topic", embedding),
            ("id2", "Python", "pattern", embedding),  # Different type
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.9)
        assert pairs == []

    def test_find_similar_pairs_different_types_allowed(self) -> None:
        """Entities of different types matched when same_type_only=False."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.9,
            same_type_only=False,  # Allow cross-type matching
            min_name_overlap=0.0,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        embedding = [1.0, 0.5, 0.3]
        entities = [
            ("id1", "Python", "topic", embedding),
            ("id2", "Python", "pattern", embedding),  # Different type
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.9)
        assert len(pairs) == 1

    def test_find_similar_pairs_name_overlap_filter(self) -> None:
        """Pairs filtered by minimum name overlap."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.9,
            same_type_only=True,
            min_name_overlap=0.5,  # Require 50% name overlap
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        embedding = [1.0, 0.5, 0.3]
        entities = [
            ("id1", "Python programming", "topic", embedding),
            ("id2", "JavaScript frameworks", "topic", embedding),  # No name overlap
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.9)
        assert pairs == []

    def test_find_similar_pairs_name_overlap_passes(self) -> None:
        """Pairs with sufficient name overlap are kept."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.9,
            same_type_only=True,
            min_name_overlap=0.3,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        embedding = [1.0, 0.5, 0.3]
        entities = [
            ("id1", "Python async programming", "topic", embedding),
            ("id2", "Python concurrent programming", "topic", embedding),
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.9)
        # "Python" and "programming" overlap, should pass
        assert len(pairs) == 1

    def test_find_similar_pairs_multiple_clusters(self) -> None:
        """Multiple duplicate clusters found correctly."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.95,
            same_type_only=False,
            min_name_overlap=0.0,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        # Two clusters of identical embeddings
        embedding_a = [1.0, 0.0, 0.0]
        embedding_b = [0.0, 1.0, 0.0]

        entities = [
            ("id1", "Cluster A 1", "topic", embedding_a),
            ("id2", "Cluster A 2", "topic", embedding_a),
            ("id3", "Cluster B 1", "topic", embedding_b),
            ("id4", "Cluster B 2", "topic", embedding_b),
        ]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.95)
        # Should find: (id1, id2) and (id3, id4)
        assert len(pairs) == 2

        pair_ids = {(p.entity1_id, p.entity2_id) for p in pairs}
        assert ("id1", "id2") in pair_ids
        assert ("id3", "id4") in pair_ids


class TestEntityDeduplicatorSuggestKeep:
    """Test the _suggest_keep heuristic."""

    def test_suggest_keep_longer_name(self) -> None:
        """Longer name is preferred."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        # Name1 is significantly longer
        result = dedup._suggest_keep("id1", "id2", "Python programming language", "Python")
        assert result == "id1"

        # Name2 is significantly longer
        result = dedup._suggest_keep("id1", "id2", "Python", "Python programming language")
        assert result == "id2"

    def test_suggest_keep_similar_length_prefers_first(self) -> None:
        """Similar length names default to first ID."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        result = dedup._suggest_keep("id1", "id2", "Python 3", "Python 2")
        assert result == "id1"


class TestEntityDeduplicatorFindDuplicates:
    """Test the full find_duplicates async workflow."""

    @pytest.mark.asyncio
    async def test_find_duplicates_insufficient_entities(self) -> None:
        """Returns empty when fewer than 2 entities."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup(
            entities={
                "id1": Entity(
                    id="id1",
                    name="Entity One",
                    entity_type=EntityType.TOPIC,
                    embedding=[1.0, 0.0],
                )
            }
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        pairs = await dedup.find_duplicates()
        assert pairs == []

    @pytest.mark.asyncio
    async def test_find_duplicates_returns_sorted_pairs(self) -> None:
        """Duplicate pairs are sorted by similarity (highest first)."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup(
            entities={
                "id1": Entity(
                    id="id1",
                    name="Python async",
                    entity_type=EntityType.TOPIC,
                    embedding=[1.0, 0.5, 0.0],
                ),
                "id2": Entity(
                    id="id2",
                    name="Python async",
                    entity_type=EntityType.TOPIC,
                    embedding=[1.0, 0.5, 0.0],
                ),
                "id3": Entity(
                    id="id3",
                    name="Python sync",
                    entity_type=EntityType.TOPIC,
                    embedding=[1.0, 0.4, 0.1],
                ),
            }
        )
        config = DedupConfig(
            similarity_threshold=0.9,
            same_type_only=True,
            min_name_overlap=0.0,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        pairs = await dedup.find_duplicates(threshold=0.9)

        # Both pairs should be found
        assert len(pairs) >= 1
        # First pair should have highest similarity
        if len(pairs) > 1:
            assert pairs[0].similarity >= pairs[1].similarity

    @pytest.mark.asyncio
    async def test_find_duplicates_with_type_filter(self) -> None:
        """Type filter is applied while staying on the entity manager seam."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup(
            entities={
                "id1": Entity(
                    id="id1",
                    name="Entity One",
                    entity_type=EntityType.TOPIC,
                    embedding=[1.0, 0.0],
                ),
                "id2": Entity(
                    id="id2",
                    name="Entity Two",
                    entity_type=EntityType.TOPIC,
                    embedding=[1.0, 0.0],
                ),
                "id3": Entity(
                    id="id3",
                    name="Entity Three",
                    entity_type=EntityType.PATTERN,
                    embedding=[1.0, 0.0],
                ),
            }
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        pairs = await dedup.find_duplicates(entity_types=["topic"])

        assert len(pairs) == 1
        assert {pairs[0].entity1_id, pairs[0].entity2_id} == {"id1", "id2"}
        assert client.query_history == []
        assert client.read_calls == []
        assert client.read_org_calls == []
        assert manager.list_all_calls[0]["include_archived"] is True

    @pytest.mark.asyncio
    async def test_find_duplicates_prefers_entity_manager_list_all(self) -> None:
        """Dedup should read candidates through the entity manager seam when available."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup(
            entities={
                "id1": Entity(
                    id="id1",
                    name="Python async",
                    entity_type=EntityType.TOPIC,
                    embedding=[1.0, 0.0, 0.0],
                ),
                "id2": Entity(
                    id="id2",
                    name="Python async programming",
                    entity_type=EntityType.TOPIC,
                    embedding=[0.99, 0.01, 0.0],
                ),
            }
        )
        config = DedupConfig(
            similarity_threshold=0.9,
            same_type_only=True,
            min_name_overlap=0.0,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        pairs = await dedup.find_duplicates(entity_types=["topic"], threshold=0.9)

        assert len(pairs) == 1
        assert pairs[0].entity1_id == "id1"
        assert client.query_history == []
        assert client.read_org_calls == []

    @pytest.mark.asyncio
    async def test_resolve_existing_entities_batches_hnsw_candidates(self) -> None:
        client = HnswDedupClient()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.95,
            batch_size=8,
            same_type_only=True,
            min_name_overlap=0.0,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        matches = await dedup.resolve_existing_entities(
            [
                Entity(
                    id="incoming_alpha",
                    name="Alpha",
                    entity_type=EntityType.TOPIC,
                    embedding=[1.0, 0.0],
                ),
                Entity(
                    id="incoming_beta",
                    name="Beta",
                    entity_type=EntityType.TOPIC,
                    embedding=[0.0, 1.0],
                ),
            ]
        )

        assert set(matches) == {"incoming_alpha", "incoming_beta"}
        assert matches["incoming_alpha"].entity2_id == "existing_alpha"
        assert matches["incoming_beta"].entity2_id == "existing_beta"
        assert len(client.raw_calls) == 1
        query, params = client.raw_calls[0]
        assert query.count("name_embedding <|") == 2
        assert params["seed_embedding_0"] == [1.0, 0.0]
        assert params["seed_embedding_1"] == [0.0, 1.0]

    @pytest.mark.asyncio
    async def test_resolve_existing_entities_falls_back_without_raw_queries(self) -> None:
        client = HnswFallbackDedupClient()
        manager = MockEntityManagerForDedup()
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        matches = await dedup.resolve_existing_entities(
            [
                Entity(
                    id="incoming_alpha",
                    name="Alpha",
                    entity_type=EntityType.TOPIC,
                    embedding=[1.0, 0.0],
                ),
                Entity(
                    id="incoming_beta",
                    name="Beta",
                    entity_type=EntityType.TOPIC,
                    embedding=[0.0, 1.0],
                ),
            ]
        )

        assert set(matches) == {"incoming_alpha", "incoming_beta"}
        assert matches["incoming_alpha"].entity2_id == "existing_alpha"
        assert matches["incoming_beta"].entity2_id == "existing_beta"
        assert len(client.calls) == 2
        assert all(query.count("name_embedding <|") == 1 for query, _ in client.calls)

    @pytest.mark.asyncio
    async def test_resolve_existing_entities_executes_surreal_hnsw_batch_query(
        self,
    ) -> None:
        client = make_graph_client("org-dedup-hnsw-batch-resolution")
        try:
            await prepare_graph_schema(client)
            manager = EntityManager(client, group_id=client.group_id)
            alpha_embedding = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]
            beta_embedding = [0.0, 1.0, *([0.0] * (EMBEDDING_DIM - 2))]
            await manager.create_direct(
                Entity(
                    id="existing_alpha_live",
                    name="Alpha",
                    entity_type=EntityType.TOPIC,
                    embedding=alpha_embedding,
                ),
                generate_embedding=False,
            )
            await manager.create_direct(
                Entity(
                    id="existing_beta_live",
                    name="Beta",
                    entity_type=EntityType.TOPIC,
                    embedding=beta_embedding,
                ),
                generate_embedding=False,
            )
            dedup = EntityDeduplicator(
                client=client,
                entity_manager=manager,
                config=DedupConfig(
                    similarity_threshold=0.95,
                    same_type_only=True,
                    min_name_overlap=0.0,
                ),
            )

            matches = await dedup.resolve_existing_entities(
                [
                    Entity(
                        id="incoming_alpha_live",
                        name="Alpha",
                        entity_type=EntityType.TOPIC,
                        embedding=alpha_embedding,
                    ),
                    Entity(
                        id="incoming_beta_live",
                        name="Beta",
                        entity_type=EntityType.TOPIC,
                        embedding=beta_embedding,
                    ),
                ]
            )

            assert matches["incoming_alpha_live"].entity2_id == "existing_alpha_live"
            assert matches["incoming_beta_live"].entity2_id == "existing_beta_live"
        finally:
            await client.close()


class TestEntityDeduplicatorMerge:
    """Test entity merge operations."""

    @pytest.mark.asyncio
    async def test_merge_entities_success(self) -> None:
        """Successful merge deletes removed entity."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()

        # Add entities
        entity1 = make_entity_for_test("id1", name="Keep Me", metadata={"key": "value1"})
        entity2 = make_entity_for_test("id2", name="Remove Me", metadata={"other": "value2"})
        manager.entities["id1"] = entity1
        manager.entities["id2"] = entity2

        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]
        mock_relationship_manager = MagicMock()
        mock_relationship_manager.get_for_entity = AsyncMock(
            return_value=[
                Relationship(
                    id="rel-out",
                    relationship_type=RelationshipType.RELATED_TO,
                    source_id="id2",
                    target_id="id3",
                    weight=0.8,
                    metadata={"reason": "semantic similarity"},
                ),
                Relationship(
                    id="rel-in",
                    relationship_type=RelationshipType.DEPENDS_ON,
                    source_id="id4",
                    target_id="id2",
                    weight=1.0,
                    metadata={"confidence": 0.9},
                ),
            ]
        )
        mock_relationship_manager.create = AsyncMock()
        mock_relationship_manager.delete = AsyncMock()
        dedup._get_relationship_manager = lambda: mock_relationship_manager  # type: ignore[method-assign]

        result = await dedup.merge_entities(keep_id="id1", remove_id="id2")

        assert result is True
        assert "id2" in manager.deleted_ids
        assert "id2" not in manager.entities
        assert client.write_org_calls == []
        mock_relationship_manager.get_for_entity.assert_awaited_once_with("id2", direction="both")
        assert mock_relationship_manager.create.await_count == 2
        assert mock_relationship_manager.delete.await_args_list == [call("rel-out"), call("rel-in")]

        redirected = [call.args[0] for call in mock_relationship_manager.create.await_args_list]
        assert redirected[0].source_id == "id1"
        assert redirected[0].target_id == "id3"
        assert redirected[0].metadata == {"reason": "semantic similarity"}
        assert redirected[1].source_id == "id4"
        assert redirected[1].target_id == "id1"
        assert redirected[1].metadata == {"confidence": 0.9}

    @pytest.mark.asyncio
    async def test_merge_entities_not_found(self) -> None:
        """Merge fails gracefully when entity not found."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()

        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        result = await dedup.merge_entities(keep_id="missing1", remove_id="missing2")

        assert result is False

    @pytest.mark.asyncio
    async def test_merge_entities_updates_cached_pairs(self) -> None:
        """Merged entities are removed from cached duplicate pairs."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()

        entity1 = make_entity_for_test("id1")
        entity2 = make_entity_for_test("id2")
        entity3 = make_entity_for_test("id3")
        manager.entities["id1"] = entity1
        manager.entities["id2"] = entity2
        manager.entities["id3"] = entity3

        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        # Set up cached pairs
        dedup._duplicate_pairs = [
            DuplicatePair("id1", "id2", 0.99),
            DuplicatePair("id2", "id3", 0.95),
        ]

        await dedup.merge_entities(keep_id="id1", remove_id="id2")

        # Pairs containing id2 should be removed
        remaining_pairs = dedup.suggest_merges()
        assert len(remaining_pairs) == 0  # Both pairs contained id2

    def test_relationship_manager_requires_graph_client(self) -> None:
        """Relationship redirects fail closed on non-native graph clients."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        dedup = EntityDeduplicator(client=client, entity_manager=manager)  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="requires a native graph client"):
            dedup._get_relationship_manager()


class TestDuplicatePair:
    """Test DuplicatePair dataclass."""

    def test_duplicate_pair_to_dict_rounds_similarity(self) -> None:
        """Similarity is rounded to 4 decimals in to_dict."""
        pair = DuplicatePair(
            entity1_id="id1",
            entity2_id="id2",
            similarity=0.987654321,
            entity1_name="Name 1",
            entity2_name="Name 2",
            entity_type="topic",
            suggested_keep="id1",
        )

        d = pair.to_dict()
        assert d["similarity"] == 0.9877


class TestGetDeduplicator:
    """Test global deduplicator factory."""

    def test_get_deduplicator_creates_new(self) -> None:
        """get_deduplicator creates new instance."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()

        dedup = get_deduplicator(client, manager)  # type: ignore[arg-type]
        assert isinstance(dedup, EntityDeduplicator)

    def test_get_deduplicator_with_custom_config(self) -> None:
        """get_deduplicator respects custom config."""
        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(similarity_threshold=0.8)

        dedup = get_deduplicator(client, manager, config=config)  # type: ignore[arg-type]
        assert dedup.config.similarity_threshold == 0.8


# =============================================================================
# Hybrid Search Tests
# =============================================================================


class TestHybridConfig:
    """Test HybridConfig defaults and customization."""

    def test_hybrid_config_defaults(self) -> None:
        """HybridConfig has sensible defaults."""
        config = HybridConfig()
        assert config.vector_weight == 1.0
        assert config.graph_weight == 0.8
        assert config.rrf_k == 60.0
        assert config.graph_depth == 2
        assert config.apply_temporal is True
        assert config.temporal_decay_days == 365.0
        assert config.apply_keyword_boost is True
        assert config.graph_expansion_only_boost == 0.45
        assert config.graph_relationship_type_weights["MENTIONS"] < 1.0
        assert config.graph_relationship_type_weights["DEPENDS_ON"] > 1.0
        assert config.reference_time is None

    def test_hybrid_config_custom(self) -> None:
        """HybridConfig accepts custom values."""
        config = HybridConfig(
            vector_weight=2.0,
            graph_weight=0.5,
            rrf_k=30.0,
            apply_temporal=False,
            apply_keyword_boost=False,
            graph_expansion_only_boost=0.2,
            graph_relationship_type_weights={"RELATED_TO": 0.7},
        )
        assert config.vector_weight == 2.0
        assert config.graph_weight == 0.5
        assert config.rrf_k == 30.0
        assert config.apply_temporal is False
        assert config.apply_keyword_boost is False
        assert config.graph_expansion_only_boost == 0.2
        assert config.graph_relationship_type_weights == {"RELATED_TO": 0.7}


class TestHybridResult:
    """Test HybridResult dataclass."""

    def test_hybrid_result_entities_property(self) -> None:
        """entities property extracts just the entities."""
        e1 = make_entity_for_test("id1")
        e2 = make_entity_for_test("id2")

        result = HybridResult(
            results=[(e1, 0.9), (e2, 0.8)],
            metadata={"query": "test"},
        )

        entities = result.entities
        assert len(entities) == 2
        assert entities[0].id == "id1"
        assert entities[1].id == "id2"

    def test_hybrid_result_total_property(self) -> None:
        """total property returns result count."""
        e1 = make_entity_for_test("id1")

        result = HybridResult(results=[(e1, 0.9)], metadata={})
        assert result.total == 1

        empty_result = HybridResult(results=[], metadata={})
        assert empty_result.total == 0


class TestVectorSearch:
    """Test vector_search function."""

    @pytest.mark.asyncio
    async def test_vector_search_calls_entity_manager(self) -> None:
        """vector_search delegates to entity_manager.search."""
        manager = MockEntityManagerForHybrid()
        e1 = make_entity_for_test("id1", name="Python")
        manager.search_results = [(e1, 0.9)]

        results = await vector_search("Python", manager, limit=10)  # type: ignore[arg-type]

        assert len(results) == 1
        assert results[0][0].id == "id1"
        assert manager.search_calls[0]["query"] == "Python"

    @pytest.mark.asyncio
    async def test_vector_search_with_type_filter(self) -> None:
        """vector_search passes entity_types filter."""
        manager = MockEntityManagerForHybrid()
        e1 = make_entity_for_test("id1", entity_type=EntityType.PATTERN)
        manager.search_results = [(e1, 0.9)]

        await vector_search(
            "test",
            manager,  # type: ignore[arg-type]
            entity_types=[EntityType.PATTERN],
            limit=5,
        )

        assert manager.search_calls[0]["entity_types"] == [EntityType.PATTERN]
        assert manager.search_calls[0]["limit"] == 5

    @pytest.mark.asyncio
    async def test_vector_search_handles_exception(self) -> None:
        """vector_search returns empty on exception."""
        manager = MockEntityManagerForHybrid()
        manager.search = AsyncMock(side_effect=Exception("DB error"))  # type: ignore[method-assign]

        results = await vector_search("test", manager)  # type: ignore[arg-type]
        assert results == []


class TestGraphTraversal:
    """Test graph_traversal function."""

    @pytest.mark.asyncio
    async def test_graph_traversal_empty_seeds(self) -> None:
        """Empty seed list returns empty results."""
        client = MockGraphClientForHybrid()

        results = await graph_traversal([], client, depth=2)  # type: ignore[arg-type]
        assert results == []

    @pytest.mark.asyncio
    async def test_graph_traversal_prefers_relationship_manager_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Graph traversal should stay on relationship-manager seams when available."""
        import sibyl_core.services.graph as graph_module

        client = make_graph_client()
        relationship_manager = MagicMock()
        near = make_entity_for_test("near", name="Near Entity")
        far = make_entity_for_test("far", name="Far Entity")
        relationship_manager.get_related_entities = AsyncMock(
            side_effect=[
                [(near, MagicMock())],
                [(far, MagicMock())],
            ]
        )

        monkeypatch.setattr(
            graph_module,
            "RelationshipManager",
            MagicMock(return_value=relationship_manager),
        )

        results = await graph_traversal(
            ["seed"],
            client,
            depth=2,
            limit=10,
            group_id="org-123",
        )  # type: ignore[arg-type]

        assert [entity.id for entity, _score in results] == ["near", "far"]
        assert results[0][1] > results[1][1]
        assert relationship_manager.get_related_entities.await_args_list == [
            call(entity_id="seed", max_depth=1, limit=50),
            call(entity_id="near", max_depth=1, limit=50),
        ]

    @pytest.mark.asyncio
    async def test_graph_traversal_initializes_relationship_manager_with_group_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Graph traversal initializes the relationship manager with explicit org scope."""
        import sibyl_core.services.graph as graph_module

        client = make_graph_client()
        relationship_manager = MagicMock()
        relationship_manager.get_related_entities = AsyncMock(return_value=[])
        relationship_manager_cls = MagicMock(return_value=relationship_manager)

        monkeypatch.setattr(
            graph_module,
            "RelationshipManager",
            relationship_manager_cls,
        )

        await graph_traversal(
            ["id1", "id2"],
            client,
            depth=3,
            limit=15,
            group_id="org-123",
        )  # type: ignore[arg-type]

        relationship_manager_cls.assert_called_once_with(client, group_id="org-123")
        assert relationship_manager.get_related_entities.await_args_list == [
            call(entity_id="id1", max_depth=1, limit=50),
            call(entity_id="id2", max_depth=1, limit=50),
        ]

    @pytest.mark.asyncio
    async def test_graph_traversal_scores_by_distance(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Graph traversal scores entities by distance."""
        import sibyl_core.services.graph as graph_module

        client = make_graph_client()
        relationship_manager = MagicMock()
        near = make_entity_for_test("near", name="Near Entity")
        far = make_entity_for_test("far", name="Far Entity")
        relationship_manager.get_related_entities = AsyncMock(
            side_effect=[
                [(near, MagicMock())],
                [(far, MagicMock())],
            ]
        )

        monkeypatch.setattr(
            graph_module,
            "RelationshipManager",
            MagicMock(return_value=relationship_manager),
        )

        results = await graph_traversal(
            ["seed"],
            client,  # type: ignore[arg-type]
            depth=2,
            group_id="org-123",
        )

        assert len(results) == 2
        # Closer entity should have higher score
        near_score = next(s for e, s in results if e.id == "near")
        far_score = next(s for e, s in results if e.id == "far")
        assert near_score > far_score  # 1/(1+1) > 1/(3+1)

    @pytest.mark.asyncio
    async def test_graph_traversal_uses_relationship_type_and_weight_signals(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Graph traversal promotes structurally strong edges over mention noise."""
        import sibyl_core.services.graph as graph_module

        client = make_graph_client()
        relationship_manager = MagicMock()
        mention_target = make_entity_for_test("mention", name="A Mention")
        dependency_target = make_entity_for_test("dependency", name="B Dependency")
        relationship_manager.get_related_entities = AsyncMock(
            return_value=[
                (
                    mention_target,
                    Relationship(
                        id="rel_mention",
                        source_id="seed",
                        target_id="mention",
                        relationship_type=RelationshipType.MENTIONS,
                    ),
                ),
                (
                    dependency_target,
                    Relationship(
                        id="rel_dependency",
                        source_id="seed",
                        target_id="dependency",
                        relationship_type=RelationshipType.DEPENDS_ON,
                        weight=2.0,
                    ),
                ),
            ]
        )

        monkeypatch.setattr(
            graph_module,
            "RelationshipManager",
            MagicMock(return_value=relationship_manager),
        )

        results = await graph_traversal(
            ["seed"],
            client,
            depth=1,
            limit=1,
            group_id="org-123",
        )  # type: ignore[arg-type]

        assert [entity.id for entity, _score in results] == ["dependency"]
        assert results[0][1] == pytest.approx(1.25)

    @pytest.mark.asyncio
    async def test_graph_traversal_keeps_strongest_same_tier_parent_edge(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A shared child keeps the strongest edge score across same-depth parents."""
        import sibyl_core.services.graph as graph_module

        client = make_graph_client()
        relationship_manager = MagicMock()
        shared = make_entity_for_test("shared", name="Shared Child")
        relationship_manager.get_related_entities = AsyncMock(
            side_effect=[
                [
                    (
                        shared,
                        Relationship(
                            id="rel_weak",
                            source_id="seed-a",
                            target_id="shared",
                            relationship_type=RelationshipType.MENTIONS,
                        ),
                    )
                ],
                [
                    (
                        shared,
                        Relationship(
                            id="rel_strong",
                            source_id="seed-b",
                            target_id="shared",
                            relationship_type=RelationshipType.DEPENDS_ON,
                            weight=2.0,
                        ),
                    )
                ],
            ]
        )

        monkeypatch.setattr(
            graph_module,
            "RelationshipManager",
            MagicMock(return_value=relationship_manager),
        )

        results = await graph_traversal(
            ["seed-a", "seed-b"],
            client,
            depth=1,
            limit=10,
            group_id="org-123",
        )  # type: ignore[arg-type]

        assert [entity.id for entity, _score in results] == ["shared"]
        assert results[0][1] == pytest.approx(1.25)

    @pytest.mark.asyncio
    async def test_graph_traversal_with_group_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Graph traversal keeps all reads on relationship-manager seams."""
        import sibyl_core.services.graph as graph_module

        client = make_graph_client()
        relationship_manager = MagicMock()
        relationship_manager.get_related_entities = AsyncMock(return_value=[])

        monkeypatch.setattr(
            graph_module,
            "RelationshipManager",
            MagicMock(return_value=relationship_manager),
        )

        await graph_traversal(["id1"], client, depth=2, group_id="org-123")  # type: ignore[arg-type]

        assert relationship_manager.get_related_entities.await_args_list == [
            call(entity_id="id1", max_depth=1, limit=50)
        ]

    @pytest.mark.asyncio
    async def test_graph_traversal_batches_relationship_frontiers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Graph traversal batches each frontier when the manager supports it."""
        import sibyl_core.services.graph as graph_module

        client = make_graph_client()
        calls: list[tuple[list[str], int]] = []
        near = make_entity_for_test("near", name="Near Entity")
        far = make_entity_for_test("far", name="Far Entity")

        class BatchRelationshipManager:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            async def get_related_entities_batch(
                self,
                entity_ids: list[str],
                *,
                limit_per_entity: int,
            ) -> dict[str, list[tuple[Entity, object]]]:
                calls.append((entity_ids, limit_per_entity))
                return {
                    "seed": [(near, object())],
                    "near": [(far, object())],
                }

        monkeypatch.setattr(
            graph_module,
            "RelationshipManager",
            BatchRelationshipManager,
        )

        results = await graph_traversal(
            ["seed"],
            client,
            depth=2,
            limit=10,
            group_id="org-123",
        )  # type: ignore[arg-type]

        assert [entity.id for entity, _score in results] == ["near", "far"]
        assert calls == [(["seed"], 50), (["near"], 50)]

    @pytest.mark.asyncio
    async def test_graph_traversal_requires_group_id(self) -> None:
        """Graph traversal fails closed without org scope."""
        client = MockGraphClientForHybrid()

        with pytest.raises(ValueError, match="group_id is required for graph traversal"):
            await graph_traversal(["id1"], client, depth=2)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_graph_traversal_requires_graph_client(self) -> None:
        """Graph traversal fails closed on non-native graph clients."""
        client = MockGraphClientForHybrid()

        with pytest.raises(RuntimeError, match="requires a native graph client"):
            await graph_traversal(
                ["id1"],
                client,
                depth=2,
                group_id="org-123",
            )  # type: ignore[arg-type]


class TestHybridSearch:
    """Test the main hybrid_search function."""

    @pytest.mark.asyncio
    async def test_hybrid_search_empty_results(self) -> None:
        """Hybrid search with no results returns empty HybridResult."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()
        manager.search_results = []

        result = await hybrid_search("test query", client, manager)  # type: ignore[arg-type]

        assert result.total == 0
        assert result.metadata["sources"] == []

    @pytest.mark.asyncio
    async def test_hybrid_search_marks_entity_manager_incomplete_on_vector_failure(
        self,
    ) -> None:
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()
        manager.search = AsyncMock(side_effect=RuntimeError("DB error"))  # type: ignore[method-assign]

        result = await hybrid_search("test query", client, manager)  # type: ignore[arg-type]

        assert result.total == 0
        assert result.metadata["entity_manager_search_completed"] is False

    @pytest.mark.asyncio
    async def test_hybrid_search_vector_only(self) -> None:
        """Hybrid search with only vector results."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        e1 = make_entity_for_test("id1", name="Python")
        manager.search_results = [(e1, 0.9)]

        config = HybridConfig(graph_weight=0)  # Disable graph

        result = await hybrid_search(
            "Python",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            config=config,
            limit=10,
        )

        assert result.total >= 1
        assert "vector" in result.metadata["sources"]

    @pytest.mark.asyncio
    async def test_hybrid_search_reports_applied_reranking(self) -> None:
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        e1 = make_entity_for_test("id1", name="Python")
        manager.search_results = [(e1, 0.9)]
        rerank_result = RerankResult(
            results=[(e1, 0.97)],
            reranked_count=1,
            model_name="test-reranker",
            metadata={"top_k": 3, "original_count": 1, "final_count": 1},
        )
        rerank = AsyncMock(return_value=rerank_result)

        with patch("sibyl_core.retrieval.reranking.rerank_results", new=rerank):
            result = await hybrid_search(
                "Python",
                client,  # type: ignore[arg-type]
                manager,  # type: ignore[arg-type]
                config=HybridConfig(
                    graph_weight=0,
                    apply_reranking=True,
                    rerank_model="test-reranker",
                    rerank_top_k=3,
                ),
            )

        assert result.metadata["reranking_applied"] is True
        assert result.metadata["reranking"] == {
            "enabled": True,
            "applied": True,
            "reranked_count": 1,
            "requested_model": "test-reranker",
            "top_k": 3,
            "original_count": 1,
            "final_count": 1,
            "model": "test-reranker",
        }
        rerank.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hybrid_search_reports_skipped_reranking(self) -> None:
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        e1 = make_entity_for_test("id1", name="Python")
        manager.search_results = [(e1, 0.9)]
        rerank_result = RerankResult(
            results=[(e1, 0.9)],
            reranked_count=0,
            model_name=None,
            metadata={"reranking_skipped": "sentence_transformers_not_installed"},
        )
        rerank = AsyncMock(return_value=rerank_result)

        with patch("sibyl_core.retrieval.reranking.rerank_results", new=rerank):
            result = await hybrid_search(
                "Python",
                client,  # type: ignore[arg-type]
                manager,  # type: ignore[arg-type]
                config=HybridConfig(
                    graph_weight=0,
                    apply_reranking=True,
                    rerank_model="test-reranker",
                    rerank_top_k=3,
                ),
            )

        assert result.metadata["reranking_applied"] is False
        assert result.metadata["reranking"] == {
            "enabled": True,
            "applied": False,
            "reranked_count": 0,
            "requested_model": "test-reranker",
            "top_k": 3,
            "reranking_skipped": "sentence_transformers_not_installed",
            "model": None,
        }

    @pytest.mark.asyncio
    async def test_hybrid_search_with_temporal_boost(self) -> None:
        """Hybrid search applies temporal boosting when enabled."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        # Recent entity
        recent = make_entity_for_test(
            "recent",
            name="Recent",
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
        # Old entity
        old = make_entity_for_test(
            "old",
            name="Old",
            created_at=datetime.now(UTC) - timedelta(days=500),
        )

        manager.search_results = [(old, 0.95), (recent, 0.9)]

        config = HybridConfig(
            apply_temporal=True,
            temporal_decay_days=30.0,  # Fast decay
            graph_weight=0,
        )

        result = await hybrid_search(
            "test",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            config=config,
        )

        assert result.metadata["temporal_applied"] is True

    @pytest.mark.asyncio
    async def test_hybrid_search_keyword_boost_promotes_matching_content(self) -> None:
        """Keyword boost can promote the answer from a shallow second place."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        noisy = make_entity_for_test("noisy", description="travel receipt")
        answer = make_entity_for_test("answer", description="phone battery tips and settings")
        manager.search_results = [(noisy, 0.9), (answer, 0.8)]

        result = await hybrid_search(
            "phone battery tips",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            config=HybridConfig(graph_weight=0, apply_temporal=False),
        )

        assert result.entities[0].id == "answer"
        assert result.metadata["keyword_boost_applied"] is True

    @pytest.mark.asyncio
    async def test_hybrid_search_keyword_boost_ignores_bookkeeping_metadata(self) -> None:
        """Keyword boost uses entity text, not system metadata labels."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        metadata_only = make_entity_for_test(
            "metadata-only",
            description="unrelated text",
            metadata={"capture_surface": "longmemeval-live"},
        )
        text_match = make_entity_for_test(
            "text-match",
            description="LongMemEval retrieval notes",
        )
        manager.search_results = [(metadata_only, 0.9), (text_match, 0.8)]

        result = await hybrid_search(
            "LongMemEval",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            config=HybridConfig(graph_weight=0, apply_temporal=False),
        )

        assert result.entities[0].id == "text-match"

    @pytest.mark.asyncio
    async def test_hybrid_search_query_coverage_rerank_promotes_relevant_top_ten(
        self,
    ) -> None:
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        distractors = [
            make_entity_for_test(f"distractor-{index}", description="generic travel planning")
            for index in range(5)
        ]
        answer = make_entity_for_test(
            "answer",
            description="bike service receipt and total maintenance expense",
        )
        tail = [
            make_entity_for_test(f"tail-{index}", description="unrelated cookbook note")
            for index in range(4)
        ]
        manager.search_results = [
            *[(entity, 1.0 - (index * 0.01)) for index, entity in enumerate(distractors)],
            (answer, 0.94),
            *[(entity, 0.8 - (index * 0.01)) for index, entity in enumerate(tail)],
        ]

        result = await hybrid_search(
            "bike service expense total",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            limit=5,
            config=HybridConfig(
                graph_weight=0,
                apply_temporal=False,
                apply_keyword_boost=False,
            ),
        )

        assert "answer" in [entity.id for entity in result.entities]
        assert result.metadata["query_coverage_rerank_applied"] is True

    @pytest.mark.asyncio
    async def test_hybrid_search_query_coverage_demotes_generic_assistant_text(
        self,
    ) -> None:
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        generic = make_entity_for_test(
            "generic",
            description="As an AI, here are some movie ideas for tonight.",
        )
        answer = make_entity_for_test(
            "answer",
            description="I recommend a comedy movie tonight.",
        )
        distractors = [
            make_entity_for_test(f"distractor-{index}", description="unrelated travel note")
            for index in range(4)
        ]
        manager.search_results = [
            (generic, 1.0),
            (answer, 0.99),
            *[(entity, 0.8 - (index * 0.01)) for index, entity in enumerate(distractors)],
        ]

        result = await hybrid_search(
            "Can you recommend a movie tonight?",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            limit=5,
            config=HybridConfig(
                graph_weight=0,
                apply_temporal=False,
                apply_keyword_boost=False,
            ),
        )

        assert result.entities[0].id == "answer"
        assert result.metadata["query_coverage_rerank_applied"] is True

    @pytest.mark.asyncio
    async def test_hybrid_search_query_coverage_uses_best_local_segment(self) -> None:
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        filler = " ".join(f"filler{index}" for index in range(24))
        distractors = [
            make_entity_for_test(
                f"distractor-{index}",
                description=f"favorite {filler} short {filler} grain {filler} rice",
            )
            for index in range(5)
        ]
        answer = make_entity_for_test(
            "answer",
            description=(
                f"{filler} {filler} I was making dinner with my favorite Japanese short grain rice."
            ),
        )
        tail = [
            make_entity_for_test(f"tail-{index}", description="unrelated cookbook note")
            for index in range(4)
        ]
        manager.search_results = [
            *[(entity, 1.0 - (index * 0.01)) for index, entity in enumerate(distractors)],
            (answer, 0.94),
            *[(entity, 0.8 - (index * 0.01)) for index, entity in enumerate(tail)],
        ]

        result = await hybrid_search(
            "What is my favorite short grain rice?",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            limit=5,
            config=HybridConfig(
                graph_weight=0,
                apply_temporal=False,
                apply_keyword_boost=False,
            ),
        )

        assert "answer" in [entity.id for entity in result.entities]
        assert result.metadata["query_coverage_rerank_applied"] is True

    @pytest.mark.asyncio
    async def test_hybrid_search_query_coverage_uses_primary_user_turn_signal(
        self,
    ) -> None:
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        distractors = [
            make_entity_for_test(
                f"distractor-{index}",
                description=(
                    "User: I need generic calendar help. Assistant: Here are tips "
                    "about what time people usually get home from work on weeknights."
                ),
            )
            for index in range(5)
        ]
        answer = make_entity_for_test(
            "answer",
            description=(
                "User: I usually get home from work around 6:30 pm on weeknights. "
                "Assistant: Here are some easy dinner ideas."
            ),
        )
        tail = [
            make_entity_for_test(f"tail-{index}", description="unrelated cookbook note")
            for index in range(3)
        ]
        manager.search_results = [
            *[(entity, 1.0 - (index * 0.01)) for index, entity in enumerate(distractors)],
            (answer, 0.92),
            *[(entity, 0.8 - (index * 0.01)) for index, entity in enumerate(tail)],
        ]

        result = await hybrid_search(
            "What time do I usually get home from work on weeknights?",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            limit=5,
            config=HybridConfig(
                graph_weight=0,
                apply_temporal=False,
                apply_keyword_boost=False,
            ),
        )

        assert "answer" in [entity.id for entity in result.entities]
        assert result.metadata["query_coverage_rerank_applied"] is True

    @pytest.mark.asyncio
    async def test_hybrid_search_query_coverage_uses_assistant_turn_for_retrospective_answer(
        self,
    ) -> None:
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        distractors = [
            make_entity_for_test(
                f"distractor-{index}",
                description=(
                    "User: I'm looking for online music resources and free lessons. "
                    "Assistant: Here are broad learning websites."
                ),
            )
            for index in range(5)
        ]
        answer = make_entity_for_test(
            "answer",
            description=(
                "User: Do you have any recommendations for learning resources? "
                "Assistant: MusicTheory.net offers free lessons and exercises "
                "for music theory."
            ),
        )
        tail = [
            make_entity_for_test(f"tail-{index}", description="User: unrelated note.")
            for index in range(4)
        ]
        manager.search_results = [
            *[(entity, 1.0 - (index * 0.01)) for index, entity in enumerate(distractors)],
            (answer, 0.92),
            *[(entity, 0.8 - (index * 0.01)) for index, entity in enumerate(tail)],
        ]

        result = await hybrid_search(
            "Can you remind me of the website you recommended for free lessons and exercises?",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            limit=5,
            config=HybridConfig(
                graph_weight=0,
                apply_temporal=False,
                apply_keyword_boost=False,
            ),
        )

        assert "answer" in [entity.id for entity in result.entities]
        assert result.metadata["query_coverage_rerank_applied"] is True

    @pytest.mark.asyncio
    async def test_hybrid_search_query_coverage_uses_product_domain_aliases(
        self,
    ) -> None:
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        generic = make_entity_for_test(
            "generic",
            description="User: I saved a general app organization note.",
        )
        answer = make_entity_for_test(
            "answer",
            description=(
                "User: I keep my portable power bank and wireless charging pad in a travel pouch."
            ),
        )
        tail = [
            make_entity_for_test(f"tail-{index}", description="User: unrelated note.")
            for index in range(8)
        ]
        manager.search_results = [
            (generic, 1.0),
            (answer, 0.99),
            *[(entity, 0.8 - (index * 0.01)) for index, entity in enumerate(tail)],
        ]

        result = await hybrid_search(
            "Any tips for better phone battery life?",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            limit=5,
            config=HybridConfig(
                graph_weight=0,
                apply_temporal=False,
                apply_keyword_boost=False,
            ),
        )

        assert result.entities[0].id == "answer"
        assert result.metadata["query_coverage_rerank_applied"] is True

    def test_hybrid_search_primary_text_ignores_assistant_turn_answers(self) -> None:
        entity = make_entity_for_test(
            "assistant-answer",
            description=(
                "User: I need generic cleaning advice. Assistant: I avoid lavender "
                "laundry detergent on towels."
            ),
        )

        primary_text = hybrid_module._entity_primary_text(entity)

        assert "generic cleaning advice" in primary_text
        assert "lavender laundry detergent" not in primary_text

    def test_hybrid_search_plain_text_is_not_primary_scoped(self) -> None:
        primary_text, has_primary_text = hybrid_module._extract_primary_text_from_text(
            "plain memory about lavender laundry detergent"
        )

        assert primary_text == "plain memory about lavender laundry detergent"
        assert has_primary_text is False

    @pytest.mark.asyncio
    async def test_hybrid_search_evidence_set_rerank_promotes_count_evidence(
        self,
    ) -> None:
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        distractors = [
            make_entity_for_test(f"distractor-{index}", description="generic travel planning")
            for index in range(5)
        ]
        answer = make_entity_for_test(
            "answer",
            description="bike service receipt and total maintenance expense",
        )
        tail = [
            make_entity_for_test(f"tail-{index}", description="unrelated cookbook note")
            for index in range(4)
        ]
        manager.search_results = [
            *[(entity, 1.0 - (index * 0.01)) for index, entity in enumerate(distractors)],
            (answer, 0.94),
            *[(entity, 0.8 - (index * 0.01)) for index, entity in enumerate(tail)],
        ]

        result = await hybrid_search(
            "How much was my bike service expense total?",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            limit=5,
            config=HybridConfig(
                graph_weight=0,
                apply_temporal=False,
                apply_keyword_boost=False,
            ),
        )

        assert "answer" in [entity.id for entity in result.entities]
        assert result.metadata["query_coverage_rerank_applied"] is True

    @pytest.mark.asyncio
    async def test_hybrid_search_evidence_set_keeps_partial_segment_from_evicting_top_evidence(
        self,
    ) -> None:
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        answers = [
            make_entity_for_test(
                f"answer-{index}",
                description=f"museum visit February evidence {index}",
            )
            for index in range(5)
        ]
        tail = make_entity_for_test(
            "tail",
            description="museum clustered with otherwise unrelated notes",
        )
        manager.search_results = [
            *[(entity, 1.0 - (index * 0.01)) for index, entity in enumerate(answers)],
            (tail, 0.94),
        ]

        result = await hybrid_search(
            "How many museum visits did I make in February?",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            limit=5,
            config=HybridConfig(
                graph_weight=0,
                apply_temporal=False,
                apply_keyword_boost=False,
            ),
        )

        assert {entity.id for entity in result.entities} == {entity.id for entity in answers}

    @pytest.mark.asyncio
    async def test_hybrid_search_uses_unfiltered_link_seeds_for_typed_results(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Typed searches can traverse from projected concepts back to source sessions."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        projected = make_entity_for_test(
            "topic_samsung",
            name="Samsung TV",
            entity_type=EntityType.TOPIC,
            description="I bought a Samsung TV for the den.",
        )
        answer = make_entity_for_test(
            "session_answer",
            name="Answer session",
            entity_type=EntityType.SESSION,
            description="Source session",
        )
        manager.search_results = [(projected, 0.9)]

        async def fake_graph_traversal(
            seed_ids: list[str],
            client: Any,
            depth: int = 2,
            limit: int = 20,
            group_id: str | None = None,
            relationship_type_weights: Any = None,
        ) -> list[tuple[Entity, float]]:
            del client, depth, limit, group_id, relationship_type_weights
            assert seed_ids == ["topic_samsung"]
            return [(answer, 0.5)]

        monkeypatch.setattr(hybrid_module, "graph_traversal", fake_graph_traversal)

        result = await hybrid_search(
            "what did I buy?",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            entity_types=[EntityType.SESSION],
            config=HybridConfig(apply_temporal=False, apply_keyword_boost=False),
        )

        assert [entity.id for entity in result.entities] == ["session_answer"]
        assert result.metadata["link_count"] == 1
        search_types = [call["entity_types"] for call in manager.search_calls]
        assert [EntityType.SESSION] in search_types
        assert None in search_types

    @pytest.mark.asyncio
    async def test_hybrid_search_skips_linking_when_typed_seeds_are_sufficient(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        sessions = [
            make_entity_for_test(
                f"session_{index}",
                name=f"Session {index}",
                entity_type=EntityType.SESSION,
            )
            for index in range(6)
        ]
        manager.search_results = [
            (session, 1.0 - index * 0.01) for index, session in enumerate(sessions)
        ]

        async def fake_graph_traversal(
            seed_ids: list[str],
            client: Any,
            depth: int = 2,
            limit: int = 20,
            group_id: str | None = None,
            relationship_type_weights: Any = None,
        ) -> list[tuple[Entity, float]]:
            del client, depth, limit, group_id, relationship_type_weights
            assert seed_ids == [session.id for session in sessions[:5]]
            return []

        monkeypatch.setattr(hybrid_module, "graph_traversal", fake_graph_traversal)

        result = await hybrid_search(
            "shift rotation",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            entity_types=[EntityType.SESSION],
            limit=5,
            config=HybridConfig(apply_temporal=False, apply_keyword_boost=False),
        )

        assert [entity.id for entity in result.entities] == [session.id for session in sessions[:5]]
        assert result.metadata["link_count"] == 0
        assert result.metadata["link_search_skipped"] is True
        assert [call["entity_types"] for call in manager.search_calls] == [[EntityType.SESSION]]

    @pytest.mark.asyncio
    async def test_hybrid_search_demotes_graph_expansion_only_results(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        answer = make_entity_for_test(
            "session_answer",
            entity_type=EntityType.SESSION,
            description="Direct session match",
        )
        projected = make_entity_for_test(
            "topic_camera",
            entity_type=EntityType.TOPIC,
            description="Projected camera preference",
        )
        distractor = make_entity_for_test(
            "session_distractor",
            entity_type=EntityType.SESSION,
            description="Expansion-only session",
        )

        async def fake_search(
            query: str,
            entity_types: list[EntityType] | None = None,
            limit: int = 10,
        ) -> list[tuple[Entity, float]]:
            manager.search_calls.append(
                {"query": query, "entity_types": entity_types, "limit": limit}
            )
            if entity_types == [EntityType.SESSION]:
                return [(answer, 0.4)]
            return [(projected, 0.9)]

        async def fake_graph_traversal(
            seed_ids: list[str],
            client: Any,
            depth: int = 2,
            limit: int = 20,
            group_id: str | None = None,
            relationship_type_weights: Any = None,
        ) -> list[tuple[Entity, float]]:
            del client, depth, limit, group_id, relationship_type_weights
            assert "topic_camera" in seed_ids
            return [(distractor, 0.9)]

        manager.search = fake_search  # type: ignore[method-assign]
        monkeypatch.setattr(hybrid_module, "graph_traversal", fake_graph_traversal)

        result = await hybrid_search(
            "camera setup",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            entity_types=[EntityType.SESSION],
            config=HybridConfig(
                graph_weight=4.0,
                graph_expansion_only_boost=0.1,
                apply_temporal=False,
                apply_keyword_boost=False,
            ),
            include_metadata=True,
        )

        assert [entity.id for entity in result.entities] == [
            "session_answer",
            "session_distractor",
        ]
        source_details = result.metadata["source_details"]
        assert source_details["session_answer"]["graph_expansion_only"] is False
        assert source_details["session_distractor"]["graph_expansion_only"] is True

    @pytest.mark.asyncio
    async def test_hybrid_search_filters_untyped_link_seeds_before_traversal(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        hidden_projected = make_entity_for_test(
            "topic_hidden",
            name="Hidden TV",
            entity_type=EntityType.TOPIC,
            metadata={"project_id": "project_hidden"},
        )
        visible_projected = make_entity_for_test(
            "topic_visible",
            name="Visible TV",
            entity_type=EntityType.TOPIC,
            metadata={"project_id": "project_visible"},
        )
        answer = make_entity_for_test(
            "session_answer",
            name="Visible source session",
            entity_type=EntityType.SESSION,
            metadata={"project_id": "project_visible"},
        )
        manager.search_results = [(hidden_projected, 0.99), (visible_projected, 0.9)]

        async def fake_graph_traversal(
            seed_ids: list[str],
            client: Any,
            depth: int = 2,
            limit: int = 20,
            group_id: str | None = None,
            relationship_type_weights: Any = None,
        ) -> list[tuple[Entity, float]]:
            del client, depth, limit, group_id, relationship_type_weights
            assert seed_ids == ["topic_visible"]
            return [(answer, 0.5)]

        monkeypatch.setattr(hybrid_module, "graph_traversal", fake_graph_traversal)

        result = await hybrid_search(
            "what did I buy?",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            entity_types=[EntityType.SESSION],
            config=HybridConfig(apply_temporal=False, apply_keyword_boost=False),
            result_filter=lambda entity: entity.metadata.get("project_id") == "project_visible",
        )

        assert [entity.id for entity in result.entities] == ["session_answer"]
        assert result.metadata["link_count"] == 1

    @pytest.mark.asyncio
    async def test_hybrid_search_temporal_reference_promotes_near_match(self) -> None:
        """Relative time in the query uses the supplied as-of timestamp."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        far = make_entity_for_test(
            "far",
            description="I bought a toaster.",
            metadata={"valid_at": "2026/01/01 00:00"},
        )
        near = make_entity_for_test(
            "near",
            description="I bought a blender.",
            metadata={"valid_at": "2026/01/11 00:00"},
        )
        manager.search_results = [(far, 0.9), (near, 0.8)]

        result = await hybrid_search(
            "What appliance did I buy 10 days ago?",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            config=HybridConfig(
                graph_weight=0,
                reference_time=datetime(2026, 1, 20, tzinfo=UTC),
            ),
        )

        assert result.entities[0].id == "near"
        assert result.metadata["temporal_target"] == "2026-01-10T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_hybrid_search_metadata_inclusion(self) -> None:
        """Hybrid search includes metadata when requested."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        e1 = make_entity_for_test("id1")
        manager.search_results = [(e1, 0.9)]

        config = HybridConfig(graph_weight=0)

        result = await hybrid_search(
            "test",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            config=config,
            include_metadata=True,
        )

        assert "source_details" in result.metadata

    @pytest.mark.asyncio
    async def test_hybrid_search_respects_limit(self) -> None:
        """Hybrid search respects result limit."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        # Many results
        manager.search_results = [
            (make_entity_for_test(f"id{i}"), 0.9 - i * 0.01) for i in range(20)
        ]

        config = HybridConfig(graph_weight=0)

        result = await hybrid_search(
            "test",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            config=config,
            limit=5,
        )

        assert result.total == 5

    @pytest.mark.asyncio
    async def test_hybrid_search_uses_entity_manager_group_id_for_graph_traversal(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Hybrid search derives org scope from the entity manager."""
        import sibyl_core.services.graph as graph_module

        client = make_graph_client()
        manager = MockEntityManagerForHybrid()
        relationship_manager = MagicMock()
        relationship_manager.get_related_entities = AsyncMock(return_value=[])
        relationship_manager_cls = MagicMock(return_value=relationship_manager)

        monkeypatch.setattr(
            graph_module,
            "RelationshipManager",
            relationship_manager_cls,
        )

        manager.search_results = [(make_entity_for_test("id1", name="Python"), 0.9)]

        await hybrid_search("Python", client, manager)  # type: ignore[arg-type]

        relationship_manager_cls.assert_called_once_with(client, group_id=manager._group_id)
        relationship_manager.get_related_entities.assert_awaited_once_with(
            entity_id="id1",
            max_depth=1,
            limit=50,
        )


class TestSimpleHybridSearch:
    """Test simple_hybrid_search function."""

    @pytest.mark.asyncio
    async def test_simple_hybrid_search_basic(self) -> None:
        """Simple hybrid search returns vector results."""
        manager = MockEntityManagerForHybrid()
        e1 = make_entity_for_test("id1")
        manager.search_results = [(e1, 0.9)]

        results = await simple_hybrid_search("test", manager, limit=10)  # type: ignore[arg-type]

        assert len(results) == 1
        assert results[0][0].id == "id1"

    @pytest.mark.asyncio
    async def test_simple_hybrid_search_with_temporal(self) -> None:
        """Simple hybrid search applies temporal boosting."""
        manager = MockEntityManagerForHybrid()

        recent = make_entity_for_test(
            "recent",
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
        old = make_entity_for_test(
            "old",
            created_at=datetime.now(UTC) - timedelta(days=365),
        )

        manager.search_results = [(old, 0.95), (recent, 0.85)]

        results = await simple_hybrid_search(
            "test",
            manager,  # type: ignore[arg-type]
            apply_temporal=True,
        )

        # Recent entity may be reordered due to temporal boost
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_simple_hybrid_search_no_temporal(self) -> None:
        """Simple hybrid search can skip temporal boosting."""
        manager = MockEntityManagerForHybrid()
        e1 = make_entity_for_test("id1")
        manager.search_results = [(e1, 0.9)]

        results = await simple_hybrid_search(
            "test",
            manager,  # type: ignore[arg-type]
            apply_temporal=False,
        )

        assert results[0][1] == 0.9  # Score unchanged


# =============================================================================
# Integration-Style Tests
# =============================================================================


class TestDedupWithRealVectors:
    """Tests using real numpy operations for vectorized similarity."""

    def test_numpy_cosine_similarity_matches_pure_python(self) -> None:
        """Numpy vectorized cosine similarity matches pure Python implementation."""
        vec1 = [1.0, 2.0, 3.0, 4.0]
        vec2 = [1.1, 2.1, 3.1, 4.1]

        # Pure Python
        python_sim = cosine_similarity(vec1, vec2)

        # Numpy
        v1 = np.array(vec1)
        v2 = np.array(vec2)
        numpy_sim = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))

        assert python_sim == pytest.approx(numpy_sim, rel=0.0001)

    def test_vectorized_finds_same_pairs_as_naive(self) -> None:
        """Vectorized implementation finds same pairs as naive O(n^2) approach."""
        entities = [
            ("id1", "Entity A", "topic", [1.0, 0.0, 0.0]),
            ("id2", "Entity B", "topic", [0.0, 1.0, 0.0]),
            ("id3", "Entity A Clone", "topic", [1.0, 0.0, 0.0]),  # Duplicate of id1
        ]

        client = MockGraphClientForDedup()
        manager = MockEntityManagerForDedup()
        config = DedupConfig(
            similarity_threshold=0.99,
            same_type_only=True,
            min_name_overlap=0.0,
        )
        dedup = EntityDeduplicator(client=client, entity_manager=manager, config=config)  # type: ignore[arg-type]

        pairs = dedup._find_similar_pairs_vectorized(entities, threshold=0.99)

        # Should find exactly one pair: (id1, id3)
        assert len(pairs) == 1
        assert pairs[0].entity1_id == "id1"
        assert pairs[0].entity2_id == "id3"


class TestHybridWithRRFFusion:
    """Test hybrid search RRF fusion behavior."""

    @pytest.mark.asyncio
    async def test_rrf_boosts_entities_in_multiple_sources(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Entities appearing in multiple sources get higher RRF scores."""
        import sibyl_core.services.graph as graph_module

        client = make_graph_client()
        manager = MockEntityManagerForHybrid()

        seed_entities = [
            make_entity_for_test(f"seed_{index}", name=f"Seed {index}") for index in range(5)
        ]
        shared_entity = make_entity_for_test("shared", name="Shared Entity")
        vector_only = make_entity_for_test("vector_only", name="Vector Only")
        graph_only = make_entity_for_test("graph_only", name="Graph Only")

        manager.search_results = [
            *[(entity, 0.99 - (index * 0.01)) for index, entity in enumerate(seed_entities)],
            (shared_entity, 0.5),
            (vector_only, 0.49),
        ]

        relationship_manager = MagicMock()
        relationship_manager.get_related_entities = AsyncMock(
            return_value=[(graph_only, MagicMock()), (shared_entity, MagicMock())]
        )

        monkeypatch.setattr(
            graph_module,
            "RelationshipManager",
            MagicMock(return_value=relationship_manager),
        )

        config = HybridConfig(
            vector_weight=1.0,
            graph_weight=1.0,
            apply_temporal=False,
        )

        result = await hybrid_search(
            "test",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            config=config,
            limit=10,
        )

        result_ids = [entity.id for entity, _score in result.results]
        assert result_ids[0] == "shared"
        assert "graph_only" in result_ids


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_cosine_similarity_large_vectors(self) -> None:
        """Cosine similarity handles large dimension vectors."""
        dim = 1536  # OpenAI embedding dimension
        vec1 = [float(i) / dim for i in range(dim)]
        vec2 = [float(i + 1) / dim for i in range(dim)]

        sim = cosine_similarity(vec1, vec2)
        assert 0.99 < sim <= 1.0  # Should be very similar

    def test_jaccard_similarity_special_characters(self) -> None:
        """Jaccard handles special characters in names."""
        sim = jaccard_similarity("C++ Programming", "C++ Development")
        assert sim > 0  # "C++" should match

    def test_jaccard_similarity_unicode(self) -> None:
        """Jaccard handles unicode characters."""
        sim = jaccard_similarity("Python", "Python")
        assert sim == 1.0

    @pytest.mark.asyncio
    async def test_hybrid_search_handles_empty_entity_id(self) -> None:
        """Hybrid search handles entities without proper IDs."""
        client = MockGraphClientForHybrid()
        manager = MockEntityManagerForHybrid()

        # Entity with empty ID
        entity = make_entity_for_test("", name="No ID Entity")
        manager.search_results = [(entity, 0.9)]

        config = HybridConfig(graph_weight=0)

        # Should not raise
        result = await hybrid_search(
            "test",
            client,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            config=config,
        )

        assert result is not None

    def test_dedup_config_validation(self) -> None:
        """DedupConfig validates threshold range."""
        # Valid thresholds
        config = DedupConfig(similarity_threshold=0.0)
        assert config.similarity_threshold == 0.0

        config = DedupConfig(similarity_threshold=1.0)
        assert config.similarity_threshold == 1.0

    @pytest.mark.asyncio
    async def test_graph_traversal_handles_manager_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Graph traversal returns empty when the relationship seam fails."""
        import sibyl_core.services.graph as graph_module

        client = make_graph_client()
        relationship_manager = MagicMock()
        relationship_manager.get_related_entities = AsyncMock(side_effect=Exception("DB error"))

        monkeypatch.setattr(
            graph_module,
            "RelationshipManager",
            MagicMock(return_value=relationship_manager),
        )

        results = await graph_traversal(
            ["id1"],
            client,
            depth=2,
            group_id="org-123",
        )  # type: ignore[arg-type]
        assert results == []
