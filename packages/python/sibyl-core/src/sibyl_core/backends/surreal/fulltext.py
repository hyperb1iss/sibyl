"""SurrealDB fulltext query helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# SurrealDB evaluates a multi-term match conjunctively when the plan is driven
# from the fulltext index: every analyzed token must appear in the matched
# field. Raw natural-language queries therefore match nothing. Sentence-shaped
# queries must go through build_fulltext_terms + build_match_disjunction, which
# issue one single-term match per (field, term) pair; single-term matches are
# plan-independent, so results are identical on the pre-3.2.1 per-row-filter
# plan and the 3.2.1+ UnionIndexScan plan.
_STOPWORDS = frozenset(
    [
        "a",
        "about",
        "after",
        "all",
        "also",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "for",
        "from",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "like",
        "me",
        "more",
        "most",
        "my",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)

_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)

# Six terms keeps every salient token of a typical goal sentence in play and
# lets BM25 IDF downweight the generic ones; the cap only bounds operator
# count (fields x terms match operators per statement).
DEFAULT_MAX_FULLTEXT_TERMS = 6


def build_fulltext_query(query: str, max_query_length: int = 128) -> str:
    sanitized = "".join(c for c in query if c.isprintable() and c not in ('"', "'")).strip()
    if not sanitized:
        return ""
    if len(sanitized) > max_query_length:
        sanitized = sanitized[:max_query_length]
    return sanitized


def build_fulltext_terms(
    query: str,
    *,
    max_terms: int = DEFAULT_MAX_FULLTEXT_TERMS,
    max_term_length: int = 32,
) -> list[str]:
    """Reduce a natural-language query to salient search terms.

    Lowercases, strips punctuation, drops stopwords and one-character tokens,
    and dedupes while preserving order. When more candidates survive than
    max_terms, longer terms win (length correlates with salience absent corpus
    statistics) with earlier position breaking ties.
    """
    sanitized = build_fulltext_query(query)
    if not sanitized:
        return []
    seen: set[str] = set()
    candidates: list[str] = []
    for raw in _TOKEN_PATTERN.findall(sanitized.lower()):
        token = raw[:max_term_length]
        if len(token) < 2 or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        candidates.append(token)
    if len(candidates) <= max_terms:
        return candidates
    ranked = sorted(enumerate(candidates), key=lambda pair: (-len(pair[1]), pair[0]))[:max_terms]
    return [token for _, token in sorted(ranked, key=lambda pair: pair[0])]


@dataclass(frozen=True)
class MatchDisjunction:
    """A per-term fulltext disjunction over one or more indexed fields.

    where_clause matches a row when any (field, term) pair matches.
    score_expr sums per-term BM25 scores within each field (BM25 is additive
    over query terms) and takes the max across fields.
    params binds one $-parameter per distinct term.
    """

    where_clause: str
    score_expr: str
    params: dict[str, str] = field(default_factory=dict)


def build_match_disjunction(
    fields: list[str],
    terms: list[str],
    *,
    param_prefix: str = "ft_term",
    operator_offset: int = 0,
) -> MatchDisjunction | None:
    """Build a conjunctive-safe match clause from pre-extracted terms.

    Assigns one match-operator index per (field, term) pair starting at
    operator_offset; search::score references use the same indices, so the
    caller must not reuse them elsewhere in the statement.
    """
    if not fields or not terms:
        return None
    params = {f"{param_prefix}{i}": term for i, term in enumerate(terms)}
    clauses: list[str] = []
    field_sums: list[str] = []
    index = operator_offset
    for column in fields:
        scores: list[str] = []
        for i in range(len(terms)):
            clauses.append(f"{column} @{index}@ ${param_prefix}{i}")
            scores.append(f"(search::score({index}) ?? 0)")
            index += 1
        field_sums.append("(" + " + ".join(scores) + ")")
    where_clause = "(" + " OR ".join(clauses) + ")"
    if len(field_sums) == 1:
        score_expr = field_sums[0]
    else:
        score_expr = "math::max([" + ", ".join(field_sums) + "])"
    return MatchDisjunction(where_clause=where_clause, score_expr=score_expr, params=params)
