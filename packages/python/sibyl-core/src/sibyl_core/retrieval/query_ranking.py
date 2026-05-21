"""Query-aware text ranking shared by runtime retrieval and eval replay."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

_KEYWORD_STOPWORDS = {
    "about",
    "also",
    "and",
    "are",
    "could",
    "did",
    "does",
    "doing",
    "during",
    "from",
    "for",
    "have",
    "having",
    "how",
    "into",
    "like",
    "many",
    "more",
    "much",
    "need",
    "please",
    "should",
    "that",
    "the",
    "there",
    "they",
    "this",
    "what",
    "when",
    "where",
    "which",
    "while",
    "with",
    "would",
    "your",
}
_RANK_WEIGHT = 0.95
_PRIOR_WEIGHT = 0.04
_OVERLAP_WEIGHT = 0.30
_DENSITY_WEIGHT = 0.08
_SEGMENT_OVERLAP_WEIGHT = 0.20
_SEGMENT_WINDOW = 18
_SEGMENT_STRIDE = 6
_GENERIC_ASSISTANT_PENALTY = 0.04
_IDF_OVERLAP_WEIGHT = 0.10
_IDF_SEGMENT_OVERLAP_WEIGHT = 0.12
_PRIMARY_OVERLAP_WEIGHT = 0.18
_PRIMARY_SEGMENT_WEIGHT = 0.20
_PHRASE_WEIGHT = 0.08
_PRIMARY_PERSONAL_WEIGHT = 0.04
_CONCEPT_OVERLAP_WEIGHT = 0.08
_PRIMARY_CONCEPT_WEIGHT = 0.06
_PREFERENCE_EVIDENCE_WEIGHT = 0.05
_EVIDENCE_SET_WINDOW = 6
_EVIDENCE_SET_MIN_OVERLAP = 0.25
_EVIDENCE_SET_INSERT_MARGIN = 0.08

_EVIDENCE_SET_QUERY_PATTERN = re.compile(
    r"\b(how many|how much|total number|number of|count of)\b",
)
_RECOMMENDATION_QUERY_PATTERN = re.compile(
    r"\b(recommend|suggest|advice|tips?|ideas?|serve|watch|choose|should i)\b",
    re.IGNORECASE,
)
_PREFERENCE_QUERY_TERMS = {
    "accessory",
    "advice",
    "advic",
    "activity",
    "choose",
    "dinner",
    "hotel",
    "idea",
    "inspiration",
    "movie",
    "recommend",
    "recommendation",
    "recipe",
    "serve",
    "show",
    "suggest",
    "suggestion",
    "tip",
    "watch",
}
_GENERIC_ASSISTANT_PATTERNS = (
    re.compile(r"\bas an ai\b"),
    re.compile(r"\bi (?:can|cannot|can't) (?:help|assist)\b"),
    re.compile(r"\bhere are (?:some|a few)\b"),
)
_TRANSCRIPT_USER_TURN_PATTERN = re.compile(
    r"user:\s*(.*?)(?=\s+assistant:|\s+user:|$)",
    re.IGNORECASE | re.DOTALL,
)
_PRIMARY_PERSONAL_PATTERN = re.compile(
    r"\b(i|i'm|i've|i'd|me|my|mine|we|our)\b",
    re.IGNORECASE,
)
_PREFERENCE_EVIDENCE_PATTERNS = (
    re.compile(
        r"\bi (?:really |usually |always |never |still |generally |normally )?"
        r"(?:prefer|like|love|enjoy|want|need|hate|dislike|avoid|use|choose)\b"
    ),
    re.compile(r"\bmy (?:favorite|preferred|ideal|usual|go-to)\b"),
    re.compile(r"\bi'm (?:fond of|a fan of|into|looking for|trying to find)\b"),
    re.compile(r"\bi tend to\b"),
)
_CONCEPT_GROUPS = (
    frozenset(
        {
            "accessory",
            "accessories",
            "battery",
            "cable",
            "charger",
            "charging",
            "phone",
            "power",
            "powerbank",
            "tech",
            "wireless",
        }
    ),
    frozenset(
        {
            "cook",
            "dinner",
            "dish",
            "fresh",
            "herb",
            "homegrown",
            "ingredient",
            "meal",
            "mint",
            "recipe",
            "serve",
        }
    ),
    frozenset(
        {
            "comedy",
            "documentary",
            "movie",
            "netflix",
            "series",
            "show",
            "special",
            "stand-up",
            "watch",
        }
    ),
    frozenset(
        {
            "city",
            "hotel",
            "room",
            "seattle",
            "travel",
            "trip",
            "view",
        }
    ),
)


@dataclass(frozen=True)
class QueryCoverageCandidate[T]:
    item: T
    stable_id: str
    text: str
    prior_score: float
    original_rank: int


@dataclass(frozen=True)
class QueryCoverageRankedCandidate[T]:
    item: T
    stable_id: str
    score: float
    original_rank: int
    overlap: float


@dataclass(frozen=True)
class QueryCoverageResult[T]:
    ranked: list[QueryCoverageRankedCandidate[T]]
    applied: bool
    changed: bool


def extract_keywords(query: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9'-]{2,}", query.lower())
    keywords: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        token = normalize_keyword_token(token)
        if token in _KEYWORD_STOPWORDS or token in seen:
            continue
        keywords.append(token)
        seen.add(token)
    return keywords


def normalize_keyword_token(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def keyword_tokens_from_text(text: str) -> list[str]:
    return [
        normalize_keyword_token(token)
        for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9'-]{2,}", text.lower())
    ]


def extract_primary_text_from_text(text: str) -> tuple[str, bool]:
    user_turns = [match.group(1) for match in _TRANSCRIPT_USER_TURN_PATTERN.finditer(text)]
    if user_turns:
        return " ".join(user_turns), True
    return text, False


def generic_assistant_marker_count(text: str) -> int:
    return sum(1 for pattern in _GENERIC_ASSISTANT_PATTERNS if pattern.search(text.lower()))


def rank_by_query_coverage[T](
    query: str,
    candidates: Sequence[QueryCoverageCandidate[T]],
) -> QueryCoverageResult[T]:
    keywords = extract_keywords(query)
    if len(keywords) < 2 or len(candidates) < 2:
        return QueryCoverageResult(
            ranked=[
                QueryCoverageRankedCandidate(
                    item=candidate.item,
                    stable_id=candidate.stable_id,
                    score=candidate.prior_score,
                    original_rank=candidate.original_rank,
                    overlap=0.0,
                )
                for candidate in candidates
            ],
            applied=False,
            changed=False,
        )

    query_terms = set(keywords)
    token_rows: list[
        tuple[
            QueryCoverageCandidate[T],
            list[str],
            set[str],
            list[str],
            set[str],
            str,
            bool,
        ]
    ] = []
    for candidate in candidates:
        text = candidate.text.lower()
        tokens = keyword_tokens_from_text(text)
        primary_text, has_primary_text = extract_primary_text_from_text(text)
        primary_tokens = keyword_tokens_from_text(primary_text) if has_primary_text else []
        token_rows.append(
            (
                candidate,
                tokens,
                set(tokens),
                primary_tokens,
                set(primary_tokens),
                primary_text,
                has_primary_text,
            )
        )

    term_weights = _query_term_weights(
        [token_set for _candidate, _tokens, token_set, *_rest in token_rows],
        query_terms,
    )
    rank_span = max(1, len(candidates) - 1)
    max_prior_score = max((candidate.prior_score for candidate in candidates), default=0.0) or 1.0
    is_preference_query = _is_preference_query(query, query_terms)
    scored: list[tuple[QueryCoverageRankedCandidate[T], int]] = []
    has_text_signal = False
    for index, (
        candidate,
        tokens,
        token_set,
        primary_tokens,
        primary_token_set,
        primary_text,
        has_primary_text,
    ) in enumerate(token_rows):
        overlap = len(query_terms & token_set) / len(query_terms)
        density = sum(1 for token in tokens if token in query_terms) / max(
            1.0,
            len(tokens) ** 0.5,
        )
        segment_overlap = _best_segment_overlap(tokens, query_terms)
        idf_overlap = _weighted_overlap(token_set, query_terms, term_weights)
        idf_segment_overlap = _best_weighted_segment_overlap(
            tokens,
            query_terms,
            term_weights,
        )
        primary_overlap = (
            _weighted_overlap(primary_token_set, query_terms, term_weights)
            if has_primary_text
            else 0.0
        )
        primary_segment_overlap = (
            _best_weighted_segment_overlap(
                primary_tokens,
                query_terms,
                term_weights,
            )
            if has_primary_text
            else 0.0
        )
        phrase_score = _phrase_adjacency_score(tokens, keywords)
        if has_primary_text:
            phrase_score = max(phrase_score, _phrase_adjacency_score(primary_tokens, keywords))
        concept_overlap = _concept_overlap_score(query_terms, token_set)
        primary_concept_overlap = (
            _concept_overlap_score(query_terms, primary_token_set) if has_primary_text else 0.0
        )
        rank_score = 1.0 - ((candidate.original_rank - 1) / rank_span)
        normalized_prior_score = (
            candidate.prior_score / max_prior_score if candidate.prior_score > 0 else 0.0
        )
        score = (
            (_RANK_WEIGHT * rank_score)
            + (_PRIOR_WEIGHT * normalized_prior_score)
            + (_OVERLAP_WEIGHT * overlap)
            + (_DENSITY_WEIGHT * density)
            + (_SEGMENT_OVERLAP_WEIGHT * segment_overlap)
            + (_IDF_OVERLAP_WEIGHT * idf_overlap)
            + (_IDF_SEGMENT_OVERLAP_WEIGHT * idf_segment_overlap)
            + (_PRIMARY_OVERLAP_WEIGHT * primary_overlap)
            + (_PRIMARY_SEGMENT_WEIGHT * primary_segment_overlap)
            + (_PHRASE_WEIGHT * phrase_score)
            + (_primary_personal_score(primary_text) if has_primary_text else 0.0)
            + (_CONCEPT_OVERLAP_WEIGHT * concept_overlap)
            + (_PRIMARY_CONCEPT_WEIGHT * primary_concept_overlap)
        )
        if is_preference_query:
            score += _PREFERENCE_EVIDENCE_WEIGHT * _preference_evidence_score(primary_text)
            score -= (
                _GENERIC_ASSISTANT_PENALTY
                * generic_assistant_marker_count(candidate.text)
                * (1.0 - min(1.0, overlap))
            )
        has_text_signal = (
            has_text_signal
            or overlap > 0.0
            or density > 0.0
            or concept_overlap > 0.0
        )
        scored.append(
            (
                QueryCoverageRankedCandidate(
                    item=candidate.item,
                    stable_id=candidate.stable_id,
                    score=score,
                    original_rank=candidate.original_rank,
                    overlap=overlap,
                ),
                index,
            )
        )

    if not has_text_signal:
        return QueryCoverageResult(
            ranked=[ranked for ranked, _index in scored],
            applied=False,
            changed=False,
        )

    if _EVIDENCE_SET_QUERY_PATTERN.search(query.lower()):
        ranked = _stabilize_evidence_set_ranking(scored)
    else:
        ranked = [
            ranked for ranked, _index in sorted(scored, key=lambda item: (-item[0].score, item[1]))
        ]
    changed = any(
        ranked_candidate.stable_id != candidates[index].stable_id
        for index, ranked_candidate in enumerate(ranked)
    )
    return QueryCoverageResult(ranked=ranked, applied=True, changed=changed)


def _query_term_weights(
    token_sets: list[set[str]],
    query_terms: set[str],
) -> dict[str, float]:
    total = max(1, len(token_sets))
    weights: dict[str, float] = {}
    for term in query_terms:
        document_frequency = sum(1 for token_set in token_sets if term in token_set)
        weights[term] = math.log((total + 1.0) / (document_frequency + 0.5)) + 1.0
    return weights


def _weighted_overlap(
    token_set: set[str],
    query_terms: set[str],
    term_weights: dict[str, float],
) -> float:
    total_weight = sum(term_weights.get(term, 1.0) for term in query_terms)
    if not token_set or not query_terms or total_weight <= 0:
        return 0.0
    return sum(term_weights.get(term, 1.0) for term in query_terms & token_set) / total_weight


def _best_segment_overlap(tokens: list[str], query_terms: set[str]) -> float:
    if not tokens or not query_terms:
        return 0.0
    if len(tokens) <= _SEGMENT_WINDOW:
        return len(query_terms & set(tokens)) / len(query_terms)

    best = 0.0
    last_start = max(0, len(tokens) - _SEGMENT_WINDOW)
    starts = list(range(0, last_start + 1, _SEGMENT_STRIDE))
    if starts[-1] != last_start:
        starts.append(last_start)
    for start in starts:
        segment = tokens[start : start + _SEGMENT_WINDOW]
        best = max(best, len(query_terms & set(segment)) / len(query_terms))
    return best


def _best_weighted_segment_overlap(
    tokens: list[str],
    query_terms: set[str],
    term_weights: dict[str, float],
) -> float:
    if not tokens or not query_terms:
        return 0.0
    if len(tokens) <= _SEGMENT_WINDOW:
        return _weighted_overlap(set(tokens), query_terms, term_weights)

    best = 0.0
    last_start = max(0, len(tokens) - _SEGMENT_WINDOW)
    starts = list(range(0, last_start + 1, _SEGMENT_STRIDE))
    if starts[-1] != last_start:
        starts.append(last_start)
    for start in starts:
        segment = tokens[start : start + _SEGMENT_WINDOW]
        best = max(best, _weighted_overlap(set(segment), query_terms, term_weights))
    return best


def _phrase_adjacency_score(tokens: list[str], query_terms: list[str]) -> float:
    if len(tokens) < 2 or len(query_terms) < 2:
        return 0.0

    query_pairs = list(pairwise(query_terms))
    token_pairs = set(pairwise(tokens))
    return sum(1 for pair in query_pairs if pair in token_pairs) / len(query_pairs)


def _primary_personal_score(primary_text: str) -> float:
    if _PRIMARY_PERSONAL_PATTERN.search(primary_text):
        return _PRIMARY_PERSONAL_WEIGHT
    return 0.0


def _preference_evidence_score(primary_text: str) -> float:
    if not primary_text:
        return 0.0
    matches = sum(1 for pattern in _PREFERENCE_EVIDENCE_PATTERNS if pattern.search(primary_text))
    return min(1.0, matches / 2.0)


def _is_preference_query(query: str, query_terms: set[str]) -> bool:
    return bool(query_terms & _PREFERENCE_QUERY_TERMS) or bool(
        _RECOMMENDATION_QUERY_PATTERN.search(query)
    )


def _concept_overlap_score(query_terms: set[str], token_set: set[str]) -> float:
    if not query_terms or not token_set:
        return 0.0

    matched = 0
    relevant = 0
    for group in _CONCEPT_GROUPS:
        if query_terms & group:
            relevant += 1
            if token_set & group:
                matched += 1
    if relevant == 0:
        return 0.0
    return matched / relevant


def _stabilize_evidence_set_ranking[T](
    scores: list[tuple[QueryCoverageRankedCandidate[T], int]],
) -> list[QueryCoverageRankedCandidate[T]]:
    window_size = min(_EVIDENCE_SET_WINDOW, len(scores))
    selected = list(scores[:window_size])
    selected_ids = {ranked.stable_id for ranked, _index in selected}
    ranked_by_coverage = sorted(scores, key=lambda item: (-item[0].score, item[1]))

    for candidate in ranked_by_coverage:
        ranked, _index = candidate
        if ranked.stable_id in selected_ids or ranked.overlap < _EVIDENCE_SET_MIN_OVERLAP:
            continue

        worst_index, worst = min(
            enumerate(selected),
            key=lambda item: (item[1][0].score, -item[1][1]),
        )
        worst_ranked, _worst_original_index = worst
        if ranked.score <= worst_ranked.score + _EVIDENCE_SET_INSERT_MARGIN:
            continue

        selected[worst_index] = candidate
        selected_ids.remove(worst_ranked.stable_id)
        selected_ids.add(ranked.stable_id)

    selected = sorted(selected, key=lambda item: (-item[0].score, item[1]))
    return [ranked for ranked, _index in selected] + [
        ranked
        for ranked, _index in ranked_by_coverage
        if ranked.stable_id not in selected_ids
    ]
