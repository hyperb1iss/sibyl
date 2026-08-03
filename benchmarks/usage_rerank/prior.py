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
        first_seen: dict[tuple[str, str], datetime] = {}
        for row in rows:
            buckets[(row.item_kind, row.item_id, row.signal_type)].append(row.event_at)
            key = (row.item_kind, row.item_id)
            known = first_seen.get(key)
            if known is None or row.event_at < known:
                first_seen[key] = row.event_at
        self._buckets = {key: sorted(values) for key, values in buckets.items()}
        self._first_seen = first_seen

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

    def first_seen_at(self, item_kind: str, item_id: str) -> datetime | None:
        """Earliest event of any signal for this item, the harness's age proxy.

        True creation time lives in the graph, not in the event table, so this is
        the best age estimate available from usage events alone. It censors any
        item that already existed when event recording began.
        """
        return self._first_seen.get((item_kind, item_id))

    def history_days_before(self, item_kind: str, item_id: str, cutoff: datetime) -> float | None:
        """How long this item had been observable before `cutoff`."""
        first = self.first_seen_at(item_kind, item_id)
        if first is None or first >= cutoff:
            return None
        return (cutoff - first).total_seconds() / 86_400.0


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


MATURE_HISTORY_DAYS = 5.0
FRESH_HISTORY_DAYS = 0.5


def describe_candidate_priors(
    labeled_sessions: Sequence[LabeledSession],
    counts: PointInTimeCounts,
) -> dict[str, Any]:
    """Compare the prior signal on cited candidates against uncited ones.

    This is what explains, or fails to explain, a what-if result. A raw count
    gap between the two groups is not on its own evidence that the counter
    carries ranking information, because retrieval_count also grows with how
    long an item has been around. The comparison therefore reports the raw
    counts, the observable history length, the age-normalized exposure rate, and
    a maturity-matched subset, so a count gap that is really an age gap is
    visible rather than inferred.
    """
    cited: list[tuple[UsageCounts, float | None]] = []
    uncited: list[tuple[UsageCounts, float | None]] = []
    for labeled in labeled_sessions:
        for item_kind in labeled.session.item_kinds:
            if not labeled.is_contrastive(item_kind):
                continue
            cutoff = labeled.session.started_at
            for item in labeled.session.items_of_kind(item_kind):
                record = (
                    counts.counts_before(item.item_kind, item.item_id, cutoff),
                    counts.history_days_before(item.item_kind, item.item_id, cutoff),
                )
                if item.key in labeled.cited_keys:
                    cited.append(record)
                elif item.key not in labeled.misled_keys:
                    uncited.append(record)

    return {
        "note": (
            "retrieval_count grows with item age, so the raw count gap and the "
            "age-normalized rate must be read together."
        ),
        "mature_history_days": MATURE_HISTORY_DAYS,
        "fresh_history_days": FRESH_HISTORY_DAYS,
        "cited": _describe_group(cited),
        "uncited": _describe_group(uncited),
    }


def _describe_group(records: Sequence[tuple[UsageCounts, float | None]]) -> dict[str, Any]:
    if not records:
        return {"candidates": 0}
    exposures = [record[0].retrieval_count for record in records]
    citations = [record[0].citation_count for record in records]
    histories = [record[1] for record in records if record[1] is not None]
    rates = [
        record[0].retrieval_count / record[1]
        for record in records
        if record[1] is not None and record[1] >= FRESH_HISTORY_DAYS
    ]
    mature = [
        record[0].retrieval_count
        for record in records
        if record[1] is not None and record[1] > MATURE_HISTORY_DAYS
    ]
    fresh = sum(1 for record in records if record[1] is None or record[1] <= FRESH_HISTORY_DAYS)
    return {
        "candidates": len(records),
        "prior_exposures_mean": round(statistics.fmean(exposures), 3),
        "prior_exposures_median": statistics.median(exposures),
        "prior_citations_mean": round(statistics.fmean(citations), 4),
        "with_prior_citation_share": round(
            sum(1 for value in citations if value > 0) / len(citations), 4
        ),
        "history_days_mean": round(statistics.fmean(histories), 3) if histories else None,
        "history_days_median": round(statistics.median(histories), 3) if histories else None,
        "fresh_share": round(fresh / len(records), 4),
        "exposures_per_day_mean": round(statistics.fmean(rates), 3) if rates else None,
        "exposures_per_day_median": round(statistics.median(rates), 3) if rates else None,
        "exposures_per_day_sample": len(rates),
        "mature_prior_exposures_mean": round(statistics.fmean(mature), 3) if mature else None,
        "mature_sample": len(mature),
    }


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
    "describe_candidate_priors",
    "permutation_null",
    "rerank_session_kind",
    "rrf_score",
    "run_whatif",
    "summarize_outcomes",
    "usage_prior_multiplier",
]
