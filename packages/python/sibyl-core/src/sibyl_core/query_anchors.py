"""Explicit query-anchor extraction and matching."""

from __future__ import annotations

import re
import threading
import unicodedata
from collections.abc import Container, Iterable
from functools import lru_cache
from typing import Any

import snowballstemmer

# Every BM25 index in the graph schema is analyzed with
# `TOKENIZERS blank, class FILTERS lowercase, ascii, snowball(english)`, and the
# MATCHES operator runs the query string through that same chain. Normalizing
# Python-side with the identical Snowball English algorithm is what keeps the
# coverage ranker looking for the term the fulltext lane actually matched.
#
# A Snowball stemmer holds the word under analysis in mutable instance state, so
# two threads sharing one instance overwrite each other's buffer and get back a
# stem of the other thread's word or an IndexError off the end of it. Coverage
# ranking is dispatched through asyncio.to_thread on both retrieval paths, which
# makes that concurrency ordinary rather than exotic, so each thread gets its
# own stemmer. Per-thread instances stay cheap and leave the hot path
# uncontended, which a lock around a shared instance would not.
_STEMMER_STATE = threading.local()


def _stemmer() -> Any:
    stemmer = getattr(_STEMMER_STATE, "instance", None)
    if stemmer is None:
        stemmer = snowballstemmer.stemmer("english")
        _STEMMER_STATE.instance = stemmer
    return stemmer


# Snowball is an inflectional stemmer and leaves graded adjectives alone: not
# one of the pairs below unifies under it (faster stays faster while fast stays
# fast), and no suffix rule recovers them safely, since stripping "er" turns
# user into us and other into oth. Adjectives whose base or inflections the
# stopword layer already classifies (new, long, late, early) are deliberately
# absent, because normalization runs before that check and would change which
# tokens it drops. Comparatives that double as everyday nouns are absent for a
# different reason: a lighter, a cleaner, a warmer and a closer are objects and
# roles, not degrees of light or clean, and folding them in makes a Zippo match
# a garage lamp. "lower" is out because it is far more often the verb. Their
# superlatives stay, since nothing reads "lightest" or "closest" as a noun.
_GRADED_ADJECTIVE_ALIASES = {
    "bigger": "big",
    "biggest": "big",
    "cheaper": "cheap",
    "cheapest": "cheap",
    "cleanest": "clean",
    "closest": "close",
    "colder": "cold",
    "coldest": "cold",
    "deeper": "deep",
    "deepest": "deep",
    "easier": "easy",
    "easiest": "easy",
    "faster": "fast",
    "fastest": "fast",
    "harder": "hard",
    "hardest": "hard",
    "heavier": "heavy",
    "heaviest": "heavy",
    "higher": "high",
    "highest": "high",
    "larger": "large",
    "largest": "large",
    "lightest": "light",
    "louder": "loud",
    "loudest": "loud",
    "lowest": "low",
    "older": "old",
    "oldest": "old",
    "quicker": "quick",
    "quickest": "quick",
    "safer": "safe",
    "safest": "safe",
    "shorter": "short",
    "shortest": "short",
    "simpler": "simple",
    "simplest": "simple",
    "slower": "slow",
    "slowest": "slow",
    "smaller": "small",
    "smallest": "small",
    "stronger": "strong",
    "strongest": "strong",
    "warmest": "warm",
    "weaker": "weak",
    "weakest": "weak",
    "wider": "wide",
    "widest": "wide",
    "younger": "young",
    "youngest": "young",
}
_TEXT_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9'-]{2,}")
_EXPLICIT_ANCHOR_PATTERN = re.compile(
    r"`(?P<backtick>[^`\n]{1,160})`"
    r'|"(?P<double>[^"\n]{1,160})"'
    r"|(?<!\w)'(?P<single>[^'\n]{2,160})'(?!\w)"
)


def _fold_ascii(token: str) -> str:
    decomposed = unicodedata.normalize("NFKD", token)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


# Ranking stems every token of every candidate, and candidate text repeats its
# vocabulary heavily, so the same handful of surface forms is stemmed thousands
# of times per search. The mapping is pure, which makes a bounded cache the
# whole of the fix; the bound keeps user text from growing it without limit.
@lru_cache(maxsize=1 << 16)
def normalize_keyword_token(token: str) -> str:
    token = _fold_ascii(token.strip("'\"").lower())
    if token.endswith("'s"):
        token = token[:-2]
    elif token.endswith("'"):
        token = token[:-1]
    return _stemmer().stemWord(_GRADED_ADJECTIVE_ALIASES.get(token, token))


def normalize_keyword_tokens(tokens: Iterable[str]) -> frozenset[str]:
    """Normalize a hand-written vocabulary so it compares against normalized tokens.

    Lexicons are authored in surface English and folded here at import time, so
    a list never has to spell out the inflections of its own entries.
    """
    return frozenset(normalize_keyword_token(token) for token in tokens)


# Snowball is inflectional and folds derivations onto the verb they came from,
# so "completely" reaches complete, "useful" and "playful" reach use and play.
# Those are adverbs and adjectives, not the act, and a sense group that admits
# them fires on text about nothing of the kind.
_DERIVATIONAL_SUFFIXES = ("ly", "ful", "ant", "ants", "ment", "ments")


def is_derivational_form(token: str, *, vocabulary: Container[str] = frozenset()) -> bool:
    """Whether a token only shares a verb's stem by derivation.

    Membership decides first: a suffix cannot tell a derived adverb from a word
    the vocabulary lists in its own right, and English is full of nouns that
    end this way ("family", "supply", "assembly"). Only a token the vocabulary
    does not know is judged by its ending.
    """
    if token in vocabulary:
        return False
    return len(token) > 5 and token.endswith(_DERIVATIONAL_SUFFIXES)


def sense_tokens_from_text(text: str, *, vocabulary: Container[str] = frozenset()) -> list[str]:
    """Tokens eligible to match a verb-sense group, derivations excluded."""
    return [
        normalize_keyword_token(token)
        for token in _TEXT_TOKEN_PATTERN.findall(text.lower())
        if not is_derivational_form(token, vocabulary=vocabulary)
    ]


def keyword_tokens_from_text(text: str) -> list[str]:
    return [normalize_keyword_token(token) for token in _TEXT_TOKEN_PATTERN.findall(text.lower())]


def keyword_and_sense_tokens_from_text(
    text: str,
    *,
    vocabulary: Container[str] = frozenset(),
) -> tuple[list[str], list[str]]:
    """Both token views of one text, from one traversal.

    The two views read the same tokens and normalize them the same way, and
    differ only in the derived forms the sense view drops, so a caller that
    wants both should not walk a fifty-thousand-character candidate twice.
    """
    keyword_tokens: list[str] = []
    sense_tokens: list[str] = []
    for surface in _TEXT_TOKEN_PATTERN.findall(text.lower()):
        normalized = normalize_keyword_token(surface)
        keyword_tokens.append(normalized)
        if not is_derivational_form(surface, vocabulary=vocabulary):
            sense_tokens.append(normalized)
    return keyword_tokens, sense_tokens


def extract_explicit_query_anchors(query: str) -> tuple[tuple[str, ...], ...]:
    return _anchor_token_groups(query, normalize=True)


def _anchor_token_groups(query: str, *, normalize: bool) -> tuple[tuple[str, ...], ...]:
    anchors: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for match in _EXPLICIT_ANCHOR_PATTERN.finditer(query):
        value = next(group for group in match.groups() if group is not None)
        tokens = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9'-]{2,}", value.lower())
        anchor = tuple(
            normalize_keyword_token(token) if normalize else token.strip("'\"")
            for token in tokens[:12]
        )
        if not anchor or anchor in seen:
            continue
        anchors.append(anchor)
        seen.add(anchor)
    return tuple(anchors)


def extract_explicit_anchor_phrases(query: str) -> tuple[tuple[str, ...], ...]:
    """Quoted anchors as the query spells them, for search queries.

    A fulltext index analyzes both sides with the same chain, so a surface
    phrase is what makes the query form and the indexed form meet. Folding
    first can only lose: graded adjectives are left alone index-side, so a
    pre-folded "fastest" stops matching the document that spells it out.
    """
    return _anchor_token_groups(query, normalize=False)


def explicit_anchor_score(
    tokens: list[str],
    anchors: tuple[tuple[str, ...], ...],
) -> float:
    if not tokens or not anchors:
        return 0.0

    token_set = set(tokens)
    scores: list[float] = []
    for anchor in anchors:
        anchor_terms = set(anchor)
        coverage = len(anchor_terms & token_set) / len(anchor_terms)
        exact = any(
            tuple(tokens[start : start + len(anchor)]) == anchor
            for start in range(len(tokens) - len(anchor) + 1)
        )
        scores.append(1.0 if exact else 0.75 if coverage == 1.0 else 0.0)

    strongest = max(scores)
    return min(1.0, (0.75 * strongest) + (0.25 * (sum(scores) / len(scores))))


def explicit_query_anchor_score(query: str, text: str) -> float:
    return explicit_anchor_score(
        keyword_tokens_from_text(text),
        extract_explicit_query_anchors(query),
    )


def explicit_query_anchor_proximity_score(query: str, text: str) -> float:
    anchors = extract_explicit_query_anchors(query)
    if len(anchors) < 2:
        return 0.0

    tokens = keyword_tokens_from_text(text)
    hits: list[tuple[int, int]] = []
    for anchor_index, anchor in enumerate(anchors):
        anchor_hits = [
            start
            for start in range(len(tokens) - len(anchor) + 1)
            if tuple(tokens[start : start + len(anchor)]) == anchor
        ]
        if not anchor_hits:
            return 0.0
        hits.extend((start, anchor_index) for start in anchor_hits)

    hits.sort()
    counts = [0] * len(anchors)
    covered = 0
    left = 0
    best_span: int | None = None
    for right_position, anchor_index in hits:
        if counts[anchor_index] == 0:
            covered += 1
        counts[anchor_index] += 1
        while covered == len(anchors):
            left_position, left_anchor_index = hits[left]
            span = right_position - left_position
            best_span = span if best_span is None else min(best_span, span)
            counts[left_anchor_index] -= 1
            if counts[left_anchor_index] == 0:
                covered -= 1
            left += 1

    return 1.0 / (1.0 + best_span) if best_span is not None else 0.0
