"""Semantic prior rescue: the coverage-gated return of the lane prior.

A gold slice that shares no vocabulary with the question reaches ranking only
through a semantic lane, carrying its cosine as ``prior_score``. The default
blend weighs that prior at 0.04, so any candidate with modest keyword overlap
outranks it. The rescue term returns the prior in proportion to how little
lexical coverage a candidate has; vocabulary-ranked candidates keep their
order and weight 0.0 is an exact no-op.
"""

from __future__ import annotations

from sibyl_core.retrieval.query_ranking import (
    QueryCoverageCandidate,
    rank_by_query_coverage,
)

QUERY = "which configuration value did the deploy pipeline change for the cache"


def _candidate(stable_id: str, text: str, prior: float, rank: int) -> QueryCoverageCandidate[str]:
    return QueryCoverageCandidate(
        item=stable_id,
        stable_id=stable_id,
        text=text,
        prior_score=prior,
        original_rank=rank,
    )


def _pool() -> list[QueryCoverageCandidate[str]]:
    return [
        _candidate(
            "vocab-strong",
            "the deploy pipeline updated the cache configuration value for staging",
            prior=0.35,
            rank=1,
        ),
        _candidate(
            "vocab-weak",
            "pipeline notes mention configuration review sessions",
            prior=0.30,
            rank=2,
        ),
        _candidate(
            # The no-vocabulary-overlap gold: found by the vector lane with the
            # pool's top cosine, lexically disjoint from the query.
            "novocab-gold",
            "TTL bumped from 300 to 900 on redis eviction settings",
            prior=0.95,
            rank=3,
        ),
        _candidate(
            "noise",
            "weekly sync agenda and attendance",
            prior=0.10,
            rank=4,
        ),
    ]


def _order(weight: float) -> list[str]:
    result = rank_by_query_coverage(
        QUERY,
        _pool(),
        semantic_prior_rescue_weight=weight,
    )
    return [row.stable_id for row in result.ranked]


def test_weight_zero_is_an_exact_noop() -> None:
    baseline = rank_by_query_coverage(QUERY, _pool())
    gated = rank_by_query_coverage(QUERY, _pool(), semantic_prior_rescue_weight=0.0)
    assert [(r.stable_id, r.score, r.overlap) for r in baseline.ranked] == [
        (r.stable_id, r.score, r.overlap) for r in gated.ranked
    ]


def test_default_blend_buries_no_vocab_gold() -> None:
    order = _order(0.0)
    assert order.index("novocab-gold") > order.index("vocab-strong")
    assert order.index("novocab-gold") > order.index("vocab-weak")


def test_rescue_lifts_no_vocab_gold_over_weak_vocab() -> None:
    order = _order(1.0)
    assert order.index("novocab-gold") < order.index("vocab-weak")
    assert order.index("novocab-gold") < order.index("noise")


def test_rescue_never_displaces_the_vocabulary_winner_in_supported_range() -> None:
    # The rescue term adds at most weight*1.0 to a zero-coverage candidate,
    # so within the supported range (the adapter caps the knob at 1.0) a
    # genuinely covered winner keeps its crown; only the low-coverage tail
    # reorders. Past 1.0 the term can outvote real vocabulary wins, which is
    # why the adapter rejects such weights.
    for weight in (0.25, 0.5, 1.0):
        order = _order(weight)
        assert order[0] == "vocab-strong", f"weight={weight} displaced the vocabulary winner"


def test_rescue_is_monotone_in_weight_for_the_gold() -> None:
    positions = [_order(weight).index("novocab-gold") for weight in (0.0, 0.25, 0.5, 1.0)]
    assert positions == sorted(positions, reverse=True) or len(set(positions)) == 1
