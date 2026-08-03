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

import math
import random
import statistics
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
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

    @property
    def earliest_event_at(self) -> datetime | None:
        """Start of the observation window, used to flag censored history."""
        return min(self._first_seen.values()) if self._first_seen else None

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
    require_contiguous_kinds: bool = True,
) -> tuple[RerankOutcome, ...]:
    """Run the what-if over every contrastive (session, item_kind) pair.

    Sessions whose item kinds interleave are dropped by default, because their
    recovered ranks are not a served order and a wrong baseline rank makes the
    measured delta meaningless for that session.
    """
    outcomes: list[RerankOutcome] = []
    for labeled in labeled_sessions:
        if require_contiguous_kinds and not labeled.session.has_contiguous_kind_blocks:
            continue
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


CENSORED_WINDOW_DAYS = 1.0
MATURE_HISTORY_DAYS = 5.0
FRESH_HISTORY_DAYS = 0.5


def describe_candidate_priors(
    labeled_sessions: Sequence[LabeledSession],
    counts: PointInTimeCounts,
    created_at_by_item: Mapping[tuple[str, str], datetime] | None = None,
    *,
    require_contiguous_kinds: bool = True,
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
    cited: list[_CandidateRecord] = []
    uncited: list[_CandidateRecord] = []
    window_start = counts.earliest_event_at
    age_checked = 0
    age_violations = 0
    for labeled in labeled_sessions:
        if require_contiguous_kinds and not labeled.session.has_contiguous_kind_blocks:
            continue
        for item_kind in labeled.session.item_kinds:
            if not labeled.is_contrastive(item_kind):
                continue
            cutoff = labeled.session.started_at
            for item in labeled.session.items_of_kind(item_kind):
                first_seen = counts.first_seen_at(item.item_kind, item.item_id)
                censored = (
                    window_start is not None
                    and first_seen is not None
                    and (first_seen - window_start).total_seconds()
                    < CENSORED_WINDOW_DAYS * 86_400.0
                )
                created_at = (
                    None if created_at_by_item is None else created_at_by_item.get(item.key)
                )
                if created_at is not None:
                    first_seen = counts.first_seen_at(item.item_kind, item.item_id)
                    age_checked += 1
                    if first_seen is not None and created_at > first_seen:
                        age_violations += 1
                true_age_days = (
                    None
                    if created_at is None or created_at >= cutoff
                    else (cutoff - created_at).total_seconds() / 86_400.0
                )
                record = _CandidateRecord(
                    counts=counts.counts_before(item.item_kind, item.item_id, cutoff),
                    history_days=counts.history_days_before(item.item_kind, item.item_id, cutoff),
                    censored=censored,
                    true_age_days=true_age_days,
                )
                if item.key in labeled.cited_keys:
                    cited.append(record)
                elif item.key not in labeled.misled_keys:
                    uncited.append(record)

    return {
        "note": (
            "retrieval_count grows with item age, so the raw count gap and the "
            "age-normalized rate must be read together. censored_share is the "
            "fraction whose history is understated because the item was already "
            "present when event recording began; a higher censored_share on the "
            "uncited group means the measured age gap is conservative."
        ),
        "mature_history_days": MATURE_HISTORY_DAYS,
        "fresh_history_days": FRESH_HISTORY_DAYS,
        "cited": _describe_group(cited),
        "uncited": _describe_group(uncited),
        # An item cannot be served before it exists, so a created_at later than
        # the item's first usage event would mean the timestamp was rewritten.
        # The entity upsert assigns created_at unconditionally
        # (services/graph.py:245), unlike created_by which preserves the existing
        # value, so this is the check that licenses using it as an age source.
        "age_source_integrity": {
            "items_checked": age_checked,
            "created_at_after_first_exposure": age_violations,
            "trustworthy": age_violations == 0,
        },
        "age_standardized": _standardize_by_age(cited, uncited),
    }


def _standardize_by_age(
    cited: Sequence[_CandidateRecord],
    uncited: Sequence[_CandidateRecord],
) -> dict[str, Any]:
    """Compare prior exposures with the age difference held fixed.

    Direct standardization: within each true-age band, take the uncited group's
    mean exposure count, then reweight those band means by how the cited group is
    distributed across bands. The result is what the uncited group would average
    if it had the cited group's ages, so the remaining ratio is the part of the
    exposure gap that age does not explain. An age-normalized rate cannot do this
    job, because dividing by age discards the candidates with the least history
    and those are exactly where the effect concentrates.
    """
    cited_aged = [record for record in cited if record.true_age_days is not None]
    uncited_aged = [record for record in uncited if record.true_age_days is not None]
    if not cited_aged or not uncited_aged:
        return {"resolved": False, "reason": "no true creation timestamps supplied"}

    bands: list[dict[str, Any]] = []
    weighted_uncited = 0.0
    weight_total = 0
    usable_cited: list[int] = []
    for low, high in AGE_BANDS_DAYS:
        cited_band = [
            record.counts.retrieval_count
            for record in cited_aged
            if low <= (record.true_age_days or 0.0) < high
        ]
        uncited_band = [
            record.counts.retrieval_count
            for record in uncited_aged
            if low <= (record.true_age_days or 0.0) < high
        ]
        band: dict[str, Any] = {
            "from_days": low,
            "to_days": None if high == float("inf") else high,
            "cited_n": len(cited_band),
            "uncited_n": len(uncited_band),
        }
        if cited_band and uncited_band:
            cited_mean = statistics.fmean(cited_band)
            uncited_mean = statistics.fmean(uncited_band)
            band["cited_mean"] = round(cited_mean, 3)
            band["uncited_mean"] = round(uncited_mean, 3)
            band["ratio"] = round(uncited_mean / cited_mean, 3) if cited_mean else None
            weighted_uncited += uncited_mean * len(cited_band)
            weight_total += len(cited_band)
            # The comparison denominator has to come from the same bands that
            # contribute to the standardized numerator. Averaging over every
            # resolved cited candidate instead would mix a population the
            # standardization never touched into the ratio.
            usable_cited.extend(cited_band)
        bands.append(band)

    cited_overall = statistics.fmean(usable_cited) if usable_cited else None
    raw_uncited = statistics.fmean([record.counts.retrieval_count for record in uncited_aged])
    standardized = weighted_uncited / weight_total if weight_total else None
    return {
        "resolved": True,
        "cited_resolved": len(cited_aged),
        "uncited_resolved": len(uncited_aged),
        "cited_true_age_days_median": round(
            statistics.median([record.true_age_days or 0.0 for record in cited_aged]), 3
        ),
        "uncited_true_age_days_median": round(
            statistics.median([record.true_age_days or 0.0 for record in uncited_aged]), 3
        ),
        "cited_exposures_mean": round(cited_overall, 3) if cited_overall is not None else None,
        "cited_usable_sample": len(usable_cited),
        "uncited_exposures_mean_raw": round(raw_uncited, 3),
        "uncited_exposures_mean_age_standardized": (
            round(standardized, 3) if standardized is not None else None
        ),
        "raw_ratio": round(raw_uncited / cited_overall, 3) if cited_overall else None,
        "age_standardized_ratio": (
            round(standardized / cited_overall, 3)
            if standardized is not None and cited_overall
            else None
        ),
        "bands": bands,
    }


AGE_BANDS_DAYS: tuple[tuple[float, float], ...] = (
    (0.0, 0.5),
    (0.5, 1.0),
    (1.0, 3.0),
    (3.0, 7.0),
    (7.0, 14.0),
    (14.0, 30.0),
    (30.0, float("inf")),
)


@dataclass(frozen=True, slots=True)
class _CandidateRecord:
    """One candidate's prior signal, age, and censoring status."""

    counts: UsageCounts
    history_days: float | None
    censored: bool
    true_age_days: float | None = None


def _describe_group(records: Sequence[_CandidateRecord]) -> dict[str, Any]:
    if not records:
        return {"candidates": 0}
    exposures = [record.counts.retrieval_count for record in records]
    citations = [record.counts.citation_count for record in records]
    histories = [record.history_days for record in records if record.history_days is not None]
    rates = [
        record.counts.retrieval_count / record.history_days
        for record in records
        if record.history_days is not None and record.history_days >= FRESH_HISTORY_DAYS
    ]
    mature = [
        record.counts.retrieval_count
        for record in records
        if record.history_days is not None and record.history_days > MATURE_HISTORY_DAYS
    ]
    fresh = sum(
        1
        for record in records
        if record.history_days is None or record.history_days <= FRESH_HISTORY_DAYS
    )
    true_ages = [record.true_age_days for record in records if record.true_age_days is not None]
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
        # Read exposures_per_day only alongside its sample size. The rate is
        # undefined for a candidate with almost no history, so this drops the
        # freshest candidates, and it drops far more of the cited group than the
        # uncited one. age_standardized is the comparison that controls for age
        # without discarding anybody.
        "exposures_per_day_mean": round(statistics.fmean(rates), 3) if rates else None,
        "exposures_per_day_median": round(statistics.median(rates), 3) if rates else None,
        "exposures_per_day_sample": len(rates),
        "exposures_per_day_retained_share": round(len(rates) / len(records), 4),
        "mature_prior_exposures_mean": round(statistics.fmean(mature), 3) if mature else None,
        "mature_sample": len(mature),
        "censored_share": round(sum(1 for record in records if record.censored) / len(records), 4),
        "true_age_days_median": round(statistics.median(true_ages), 3) if true_ages else None,
        "true_age_days_mean": round(statistics.fmean(true_ages), 3) if true_ages else None,
        "true_age_resolved": len(true_ages),
    }


def describe_population(
    labeled_sessions: Sequence[LabeledSession],
    *,
    require_contiguous_kinds: bool = True,
) -> dict[str, Any]:
    """Account for every session between "contrastive" and "actually ranked".

    Two independent exclusions sit between those two counts and reporting only
    the endpoints hides one of them. A session is dropped when its kinds
    interleave, because its recovered ranks are not a served order, and
    separately when no single item kind is contrastive on its own, because a
    positive in one kind and the negatives in another cannot be compared.
    """
    contrastive = [labeled for labeled in labeled_sessions if labeled.is_contrastive()]
    contiguous = [
        labeled
        for labeled in contrastive
        if not require_contiguous_kinds or labeled.session.has_contiguous_kind_blocks
    ]
    ranked_lists = [
        (labeled, item_kind)
        for labeled in contiguous
        for item_kind in labeled.session.item_kinds
        if labeled.is_contrastive(item_kind)
    ]
    ranked_sessions = {labeled.session.ref for labeled, _ in ranked_lists}
    return {
        "contrastive_sessions": len(contrastive),
        "dropped_interleaved_kinds": len(contrastive) - len(contiguous),
        "after_interleaved_exclusion": len(contiguous),
        "dropped_no_single_kind_contrastive": len(contiguous) - len(ranked_sessions),
        "ranked_sessions": len(ranked_sessions),
        "ranked_lists": len(ranked_lists),
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
    require_contiguous_kinds: bool = True,
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
        if require_contiguous_kinds and not labeled.session.has_contiguous_kind_blocks:
            continue
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
        return {
            "trials": 0,
            "mean": None,
            "stdev": None,
            "p95_abs": None,
            "interpretation": "no contrastive population to build a null from",
        }

    baseline_rr = [
        1.0 / arm_scores[index].baseline_rank for arm_scores, cited in arms for index in cited
    ]
    baseline_mrr = statistics.fmean(baseline_rr)

    deltas: list[float] = []
    for _ in range(trials):
        trial_rr: list[float] = []
        for arm_scores, cited in arms:
            multipliers = [entry.multiplier for entry in arm_scores]
            rng.shuffle(multipliers)
            shuffled = [
                ExposedItemScore(
                    item_id=entry.item_id,
                    baseline_rank=entry.baseline_rank,
                    multiplier=multipliers[index],
                )
                for index, entry in enumerate(arm_scores)
            ]
            ordered = sorted(
                shuffled,
                key=lambda entry: (
                    -rrf_score(entry.baseline_rank) * entry.multiplier,
                    entry.baseline_rank,
                ),
            )
            new_rank = {entry.item_id: index + 1 for index, entry in enumerate(ordered)}
            trial_rr.extend(1.0 / new_rank[arm_scores[index].item_id] for index in cited)
        deltas.append(statistics.fmean(trial_rr) - baseline_mrr)

    return {
        "trials": trials,
        "seed": seed,
        "mean": round(statistics.fmean(deltas), 6),
        "stdev": round(statistics.stdev(deltas), 6) if len(deltas) > 1 else 0.0,
        "min": round(min(deltas), 6),
        "max": round(max(deltas), 6),
        "p95_abs": round(_nearest_rank_percentile([abs(delta) for delta in deltas], 0.95), 6),
        "interpretation": (
            "distance from a same-strength random prior, not a two-sided floor "
            "around zero: the null is centred well below zero because any "
            "reordering degrades an already-good baseline. Use "
            "bootstrap_ci_vs_zero to ask whether an arm beat the baseline."
        ),
    }


def _nearest_rank_percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile, so p95 of 200 samples is the 190th smallest."""
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def bootstrap_ci_vs_zero(
    outcomes: Sequence[RerankOutcome],
    *,
    resamples: int = 5000,
    seed: int = 20260803,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """Paired bootstrap on the per-item reciprocal-rank change.

    This is the test that answers the question the verdict actually asks, which
    is whether an arm beat the served baseline. The permutation null answers a
    different question, whether the prior beat a random prior of equal strength,
    and its distribution is centred far below zero because any reordering hurts a
    good baseline. Reading a one-sided null as a two-sided floor around zero
    conflates the two.
    """
    if not outcomes:
        return {"resamples": 0, "observed": None, "ci_low": None, "ci_high": None}
    deltas = [
        (1.0 / outcome.reweighted_rank) - (1.0 / outcome.baseline_rank) for outcome in outcomes
    ]
    observed = statistics.fmean(deltas)
    rng = random.Random(seed)
    size = len(deltas)
    means: list[float] = []
    for _ in range(resamples):
        sample = [deltas[rng.randrange(size)] for _ in range(size)]
        means.append(statistics.fmean(sample))
    means.sort()
    tail = (1.0 - confidence) / 2.0
    low = _nearest_rank_percentile(means, tail)
    high = _nearest_rank_percentile(means, 1.0 - tail)
    at_or_below_zero = sum(1 for value in means if value <= 0.0) / resamples
    return {
        "resamples": resamples,
        "seed": seed,
        "confidence": confidence,
        "observed": round(observed, 6),
        "ci_low": round(low, 6),
        "ci_high": round(high, 6),
        "share_at_or_below_zero": round(at_or_below_zero, 4),
        "excludes_zero": low > 0.0 or high < 0.0,
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
    "AGE_BANDS_DAYS",
    "CITATION_WEIGHT",
    "MISLED_WEIGHT",
    "RETRIEVAL_WEIGHT",
    "RRF_K",
    "ExposedItemScore",
    "PointInTimeCounts",
    "RerankOutcome",
    "UsageCounts",
    "bootstrap_ci_vs_zero",
    "describe_candidate_priors",
    "describe_population",
    "permutation_null",
    "rerank_session_kind",
    "rrf_score",
    "run_whatif",
    "summarize_outcomes",
    "usage_prior_multiplier",
]
