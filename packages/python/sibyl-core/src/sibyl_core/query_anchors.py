"""Explicit query-anchor extraction and matching."""

from __future__ import annotations

import re

_NORMALIZED_TOKEN_ALIASES = {
    "attended": "attend",
    "attending": "attend",
    "assembled": "assemble",
    "assembling": "assemble",
    "classes": "class",
    "engaged": "engagement",
    "fixed": "fix",
    "fixing": "fix",
    "presented": "present",
    "presenting": "present",
    "relied": "rely",
    "relying": "rely",
    "serviced": "service",
    "servicing": "service",
    "sold": "sell",
    "selling": "sell",
    "subscribed": "subscription",
    "subscribing": "subscription",
    "volunteered": "volunteer",
    "volunteering": "volunteer",
}
# Comparatives and superlatives are the same lemma as their base adjective, and
# no suffix rule recovers that safely: stripping "er" turns user into us and
# other into oth. Adjectives whose base or inflections the stopword layer
# already classifies (new, long, late, early) are deliberately absent, because
# normalization runs before that check and would change which tokens it drops.
# Comparatives that double as everyday nouns are absent for a different reason:
# a lighter, a cleaner, a warmer and a closer are objects and roles, not degrees
# of light or clean, and folding them in makes a Zippo match a garage lamp.
# "lower" is out because it is far more often the verb. Their superlatives stay,
# since nothing reads "lightest" or "closest" as a noun.
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
_EXPLICIT_ANCHOR_PATTERN = re.compile(
    r"`(?P<backtick>[^`\n]{1,160})`"
    r'|"(?P<double>[^"\n]{1,160})"'
    r"|(?<!\w)'(?P<single>[^'\n]{2,160})'(?!\w)"
)


def normalize_keyword_token(token: str) -> str:
    token = token.strip("'\"")
    if token in _NORMALIZED_TOKEN_ALIASES:
        return _NORMALIZED_TOKEN_ALIASES[token]
    if token in _GRADED_ADJECTIVE_ALIASES:
        return _GRADED_ADJECTIVE_ALIASES[token]
    if len(token) > 4 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 4 and token.endswith(("ches", "shes", "xes", "zes")):
        return token[:-2]
    if len(token) > 4 and token.endswith(("ces", "ses")):
        return token[:-1]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("is", "ous", "ss", "us")):
        return token[:-1]
    return token


def keyword_tokens_from_text(text: str) -> list[str]:
    return [
        normalize_keyword_token(token)
        for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9'-]{2,}", text.lower())
    ]


def extract_explicit_query_anchors(query: str) -> tuple[tuple[str, ...], ...]:
    anchors: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for match in _EXPLICIT_ANCHOR_PATTERN.finditer(query):
        value = next(group for group in match.groups() if group is not None)
        anchor = tuple(keyword_tokens_from_text(value)[:12])
        if not anchor or anchor in seen:
            continue
        anchors.append(anchor)
        seen.add(anchor)
    return tuple(anchors)


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
