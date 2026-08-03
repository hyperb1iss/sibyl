"""Usage-prior reweighting and the offline rerank what-if.

The multiplier reuses the shape already shipped in production for retention,
`usage_retention_multiplier` in
packages/python/sibyl-core/src/sibyl_core/retrieval/temporal.py:196-205: capped
linear bonuses for retrieval and citation counts, a heavier capped penalty for
misled counts, clamped to [0.1, 4.0]. Borrowing the shape keeps the feasibility
question honest, because a production change would most plausibly reuse the
same curve rather than invent a new one for ranking.

Two properties make the measurement trustworthy:

* Counts are point-in-time. Every count for an item at session S is computed
  from events strictly before S began, so the citation the what-if is trying to
  promote can never be an input to the score that promotes it. A leaky version
  of this analysis reports a huge win and means nothing.
* Baseline scores come from the served rank via the production RRF curve
  (retrieval/fusion.py:54), because the fused score is not persisted on the
  event. It is a proxy for the real score, and it is monotone in rank, which is
  all the reordering needs.
"""

from __future__ import annotations

import random
import statistics
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from events import CITATION, EXPOSURE, MISLED, UsageEventRow
from join import LabeledSession

RRF_K = 60.0

RETRIEVAL_CAP = 50
RETRIEVAL_WEIGHT = 0.02
CITATION_CAP = 20
CITATION_WEIGHT = 0.12
MISLED_CAP = 5
MISLED_WEIGHT = 0.6
MULTIPLIER_FLOOR = 0.1
MULTIPLIER_CEILING = 4.0


@dataclass(frozen=True, slots=True)
class UsageCounts:
    """Usage counters for one item as of some cutoff."""

    retrieval_count: int = 0
    citation_count: int = 0
    misled_count: int = 0

    @property
    def is_empty(self) -> bool:
        return not (self.retrieval_count or self.citation_count or self.misled_count)


def rrf_score(rank: int, k: float = RRF_K) -> float:
    """Mirror of sibyl_core.retrieval.fusion.rrf_score for offline replay."""
    return 1.0 / (k + rank)


def usage_prior_multiplier(
    counts: UsageCounts,
    *,
    retrieval_weight: float = RETRIEVAL_WEIGHT,
    citation_weight: float = CITATION_WEIGHT,
    misled_weight: float = MISLED_WEIGHT,
) -> float:
    """Bounded usage multiplier, shaped after usage_retention_multiplier."""
    retrieval_bonus = min(max(counts.retrieval_count, 0), RETRIEVAL_CAP) * retrieval_weight
    citation_bonus = min(max(counts.citation_count, 0), CITATION_CAP) * citation_weight
    misled_penalty = min(max(counts.misled_count, 0), MISLED_CAP) * misled_weight
    raw = 1.0 + retrieval_bonus + citation_bonus - misled_penalty
    return max(MULTIPLIER_FLOOR, min(MULTIPLIER_CEILING, raw))


class PointInTimeCounts:
    """Answers "what were this item's usage counts before time T".

    Timestamps per (item_kind, item_id, signal) are sorted once so each lookup
    is a binary search, which matters because the what-if asks the question once
    per candidate per session.
    """

    def __init__(self, rows: Iterable[UsageEventRow]) -> None:
        buckets: dict[tuple[str, str, str], list[datetime]] = defaultdict(list)
        for row in rows:
            buckets[(row.item_kind, row.item_id, row.signal_type)].append(row.event_at)
        self._buckets = {key: sorted(values) for key, values in buckets.items()}

    def _count_before(self, item_kind: str, item_id: str, signal: str, cutoff: datetime) -> int:
        timestamps = self._buckets.get((item_kind, item_id, signal))
        if not timestamps:
            return 0
        return bisect_left(timestamps, cutoff)

    def counts_before(self, item_kind: str, item_id: str, cutoff: datetime) -> UsageCounts:
        return UsageCounts(
            retrieval_count=self._count_before(item_kind, item_id, EXPOSURE, cutoff),
            citation_count=self._count_before(item_kind, item_id, CITATION, cutoff),
            misled_count=self._count_before(item_kind, item_id, MISLED, cutoff),
        )


@dataclass(frozen=True, slots=True)
class RerankOutcome:
    """What a usage-prior reweighting would have done to one cited item."""

    session_key: str
    source_surface: str
    item_kind: str
    item_id: str
    candidate_count: int
    baseline_rank: int
    reweighted_rank: int
    prior_multiplier: float
    prior_counts: UsageCounts
    origin: str

    @property
    def rank_delta(self) -> int:
        """Positive when the reweighting moved the cited item closer to rank 1."""
        return self.baseline_rank - self.reweighted_rank

    def to_json(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "source_surface": self.source_surface,
            "item_kind": self.item_kind,
            "item_id": self.item_id,
            "candidate_count": self.candidate_count,
            "baseline_rank": self.baseline_rank,
            "reweighted_rank": self.reweighted_rank,
            "rank_delta": self.rank_delta,
            "prior_multiplier": round(self.prior_multiplier, 6),
            "prior_retrieval_count": self.prior_counts.retrieval_count,
            "prior_citation_count": self.prior_counts.citation_count,
            "prior_misled_count": self.prior_counts.misled_count,
            "origin": self.origin,
        }


def rerank_session_kind(
    labeled: LabeledSession,
    item_kind: str,
    counts: PointInTimeCounts,
    *,
    retrieval_weight: float = RETRIEVAL_WEIGHT,
    citation_weight: float = CITATION_WEIGHT,
    misled_weight: float = MISLED_WEIGHT,
) -> tuple[RerankOutcome, ...]:
    """Reorder one kind's candidates by usage-weighted score.

    Ties keep baseline order, so an item never appears to move on the strength
    of a tie-break alone.
    """
    candidates = labeled.session.items_of_kind(item_kind)
    if not candidates:
        return ()
    cutoff = labeled.session.started_at

    scored: list[tuple[float, int, str, float, UsageCounts]] = []
    for item in candidates:
        prior_counts = counts.counts_before(item.item_kind, item.item_id, cutoff)
        multiplier = usage_prior_multiplier(
            prior_counts,
            retrieval_weight=retrieval_weight,
            citation_weight=citation_weight,
            misled_weight=misled_weight,
        )
        baseline = rrf_score(item.rank_within_kind)
        scored.append(
            (baseline * multiplier, item.rank_within_kind, item.item_id, multiplier, prior_counts)
        )

    reordered = sorted(scored, key=lambda entry: (-entry[0], entry[1]))
    new_rank_by_item = {entry[2]: index + 1 for index, entry in enumerate(reordered)}
    detail_by_item = {entry[2]: (entry[3], entry[4]) for entry in scored}

    outcomes: list[RerankOutcome] = []
    for item in candidates:
        if item.key not in labeled.cited_keys:
            continue
        multiplier, prior_counts = detail_by_item[item.item_id]
        outcomes.append(
            RerankOutcome(
                session_key=labeled.session.session_key,
                source_surface=labeled.session.source_surface,
                item_kind=item.item_kind,
                item_id=item.item_id,
                candidate_count=len(candidates),
                baseline_rank=item.rank_within_kind,
                reweighted_rank=new_rank_by_item[item.item_id],
                prior_multiplier=multiplier,
                prior_counts=prior_counts,
                origin=labeled.origin,
            )
        )
    return tuple(outcomes)


def run_whatif(
    labeled_sessions: Sequence[LabeledSession],
    counts: PointInTimeCounts,
    *,
    retrieval_weight: float = RETRIEVAL_WEIGHT,
    citation_weight: float = CITATION_WEIGHT,
    misled_weight: float = MISLED_WEIGHT,
) -> tuple[RerankOutcome, ...]:
    """Run the what-if over every contrastive (session, item_kind) pair."""
    outcomes: list[RerankOutcome] = []
    for labeled in labeled_sessions:
        for item_kind in labeled.session.item_kinds:
            if not labeled.is_contrastive(item_kind):
                continue
            outcomes.extend(
                rerank_session_kind(
                    labeled,
                    item_kind,
                    counts,
                    retrieval_weight=retrieval_weight,
                    citation_weight=citation_weight,
                    misled_weight=misled_weight,
                )
            )
    return tuple(outcomes)


def permutation_null(
    labeled_sessions: Sequence[LabeledSession],
    counts: PointInTimeCounts,
    *,
    trials: int = 200,
    seed: int = 20260803,
    retrieval_weight: float = RETRIEVAL_WEIGHT,
    citation_weight: float = CITATION_WEIGHT,
    misled_weight: float = MISLED_WEIGHT,
) -> dict[str, Any]:
    """Build a null distribution for the MRR delta by shuffling the prior.

    The multipliers a session actually earned are permuted among that session's
    candidates, which keeps the magnitude of the reweighting intact and destroys
    only its association with the item. Any reordering of ten candidates moves
    ranks around, so the question is never "did the delta move" but "did it move
    further than a meaningless prior of the same strength would have". Without
    this the campaign's sub-noise rule has nothing to compare against.
    """
    rng = random.Random(seed)
    arms: list[tuple[tuple[ExposedItemScore, ...], tuple[int, ...]]] = []
    for labeled in labeled_sessions:
        for item_kind in labeled.session.item_kinds:
            if not labeled.is_contrastive(item_kind):
                continue
            candidates = labeled.session.items_of_kind(item_kind)
            cutoff = labeled.session.started_at
            scored: list[ExposedItemScore] = []
            cited_indexes: list[int] = []
            for index, item in enumerate(candidates):
                prior_counts = counts.counts_before(item.item_kind, item.item_id, cutoff)
                multiplier = usage_prior_multiplier(
                    prior_counts,
                    retrieval_weight=retrieval_weight,
                    citation_weight=citation_weight,
                    misled_weight=misled_weight,
                )
                scored.append(
                    ExposedItemScore(
                        item_id=item.item_id,
                        baseline_rank=item.rank_within_kind,
                        multiplier=multiplier,
                    )
                )
                if item.key in labeled.cited_keys:
                    cited_indexes.append(index)
            if cited_indexes:
                arms.append((tuple(scored), tuple(cited_indexes)))

    if not arms:
        return {"trials": 0, "mean": None, "stdev": None, "p95_abs": None}

    baseline_rr = [1.0 / scored[index].baseline_rank for scored, cited in arms for index in cited]
    baseline_mrr = statistics.fmean(baseline_rr)

    deltas: list[float] = []
    for _ in range(trials):
        trial_rr: list[float] = []
        for scored, cited in arms:
            multipliers = [entry.multiplier for entry in scored]
            rng.shuffle(multipliers)
            shuffled = [
                ExposedItemScore(
                    item_id=entry.item_id,
                    baseline_rank=entry.baseline_rank,
                    multiplier=multipliers[index],
                )
                for index, entry in enumerate(scored)
            ]
            ordered = sorted(
                shuffled,
                key=lambda entry: (
                    -rrf_score(entry.baseline_rank) * entry.multiplier,
                    entry.baseline_rank,
                ),
            )
            new_rank = {entry.item_id: index + 1 for index, entry in enumerate(ordered)}
            trial_rr.extend(1.0 / new_rank[scored[index].item_id] for index in cited)
        deltas.append(statistics.fmean(trial_rr) - baseline_mrr)

    return {
        "trials": trials,
        "seed": seed,
        "mean": round(statistics.fmean(deltas), 6),
        "stdev": round(statistics.stdev(deltas), 6) if len(deltas) > 1 else 0.0,
        "min": round(min(deltas), 6),
        "max": round(max(deltas), 6),
        "p95_abs": round(sorted(abs(delta) for delta in deltas)[int(0.95 * len(deltas))], 6)
        if deltas
        else None,
    }


@dataclass(frozen=True, slots=True)
class ExposedItemScore:
    """Flat candidate record used by the permutation null."""

    item_id: str
    baseline_rank: int
    multiplier: float


def summarize_outcomes(outcomes: Sequence[RerankOutcome]) -> dict[str, Any]:
    """Aggregate the rank-delta distribution and reciprocal-rank change."""
    if not outcomes:
        return {
            "cited_items_evaluated": 0,
            "improved": 0,
            "unchanged": 0,
            "worsened": 0,
            "mean_rank_delta": None,
            "median_rank_delta": None,
            "baseline_mrr": None,
            "reweighted_mrr": None,
            "mrr_delta": None,
            "informative_prior_share": None,
        }

    deltas = [outcome.rank_delta for outcome in outcomes]
    baseline_rr = [1.0 / outcome.baseline_rank for outcome in outcomes]
    reweighted_rr = [1.0 / outcome.reweighted_rank for outcome in outcomes]
    informative = sum(1 for outcome in outcomes if not outcome.prior_counts.is_empty)
    baseline_mrr = statistics.fmean(baseline_rr)
    reweighted_mrr = statistics.fmean(reweighted_rr)
    return {
        "cited_items_evaluated": len(outcomes),
        "improved": sum(1 for delta in deltas if delta > 0),
        "unchanged": sum(1 for delta in deltas if delta == 0),
        "worsened": sum(1 for delta in deltas if delta < 0),
        "mean_rank_delta": round(statistics.fmean(deltas), 4),
        "median_rank_delta": round(statistics.median(deltas), 4),
        "min_rank_delta": min(deltas),
        "max_rank_delta": max(deltas),
        "baseline_mrr": round(baseline_mrr, 6),
        "reweighted_mrr": round(reweighted_mrr, 6),
        "mrr_delta": round(reweighted_mrr - baseline_mrr, 6),
        "informative_prior_share": round(informative / len(outcomes), 6),
    }


__all__ = [
    "CITATION_WEIGHT",
    "MISLED_WEIGHT",
    "RETRIEVAL_WEIGHT",
    "RRF_K",
    "ExposedItemScore",
    "PointInTimeCounts",
    "RerankOutcome",
    "UsageCounts",
    "permutation_null",
    "rerank_session_kind",
    "rrf_score",
    "run_whatif",
    "summarize_outcomes",
    "usage_prior_multiplier",
]
