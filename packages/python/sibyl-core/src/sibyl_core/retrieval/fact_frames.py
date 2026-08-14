"""Typed fact frames for query/evidence matching."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from sibyl_core.query_anchors import (
    is_derivational_form,
    normalize_keyword_token,
    normalize_keyword_tokens,
)

_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9'-]{1,}")
_SPAN_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+|\n+")
_FIRST_PERSON_PATTERN = re.compile(r"\b(?:i|i'm|i've|i'd|me|my|mine|we|our)\b", re.I)
_PREFERENCE_PATTERN = re.compile(
    r"\b(?:i (?:really |usually |always |never |still |generally |normally )?"
    r"(?:prefer|like|love|enjoy|want|need|hate|dislike|avoid|choose)|"
    r"my (?:favorite|preferred|ideal|usual|go-to)|"
    r"i'm (?:fond of|a fan of|into|looking for|trying to find)|"
    r"i tend to)\b",
    re.I,
)
_PROFILE_PATTERN = re.compile(
    r"\b(?:i(?:'m| am| work| study| research) (?:working in|working on|"
    r"researching|studying|specializing in|focused on|in the field)|"
    r"my (?:work|research|field|specialty|profession|job|role))\b",
    re.I,
)
_RECENCY_PATTERN = re.compile(r"\b(?:lately|recently|currently|now|these days)\b", re.I)
_SERVICE_USE_PATTERN = re.compile(
    r"\b(?:using|use|uses|via|through|subscribed to|relying on|"
    r"listening to|watching|playing|following)\s+(?:my|the|a|an|their|our)?"
    r"\s*[A-Z0-9][A-Za-z0-9&'.-]{2,}(?:\s+[A-Z0-9][A-Za-z0-9&'.-]{2,}){0,3}\b"
    r"|\b(?:listening|watching|streaming|playing|reading)\b[^.?!]{0,80}"
    r"\b(?:on|via|through)\s+"
    r"(?!(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
    r"January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\b)"
    r"[A-Z0-9][A-Za-z0-9&'.-]{2,}(?:\s+[A-Z0-9][A-Za-z0-9&'.-]{2,}){0,3}\b"
)

# Function words are matched as written, never by stem: folding the list
# would turn each entry into its whole family and swallow content words
# (differ would take difference, assist would take assistance). Countable
# nouns therefore carry their plural explicitly. "using" is absent on
# purpose, since it carries the use action.
_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "am",
    "and",
    "any",
    "are",
    "assistant",
    "assistants",
    "been",
    "before",
    "can",
    "could",
    "did",
    "different",
    "do",
    "for",
    "from",
    "have",
    "her",
    "how",
    "i've",
    "ive",
    "in",
    "into",
    "just",
    "last",
    "lately",
    "me",
    "mine",
    "month",
    "months",
    "more",
    "much",
    "my",
    "name",
    "names",
    "need",
    "needs",
    "new",
    "on",
    "or",
    "our",
    "out",
    "recent",
    "recently",
    "some",
    "that",
    "the",
    "their",
    "this",
    "today",
    "to",
    "up",
    "user",
    "users",
    "was",
    "week",
    "weeks",
    "what",
    "when",
    "which",
    "who",
    "with",
    "year",
    "years",
    "you",
}

# Every group is written in surface English and stemmed at import, so a term
# covers its own inflections. Irregular pasts (bought, got, went) survive
# stemming unchanged and are therefore listed beside the verb they belong to,
# and "service" sits under repair because that is a sense mapping the stemmer
# has no opinion about.
_ACTION_TERM_SURFACES: dict[str, frozenset[str]] = {
    "acquire": frozenset(
        {
            "acquire",
            "bought",
            "buy",
            "get",
            "got",
            "invest",
            "order",
            "pick",
            "purchase",
        }
    ),
    "attend": frozenset({"attend", "join", "participate", "visit", "went"}),
    "complete": frozenset({"complete", "finish"}),
    "create": frozenset({"build", "compose", "create", "draft", "generate", "make", "write"}),
    "present": frozenset({"present", "presentation"}),
    "profile": frozenset({"field", "focus", "profession", "research", "role", "specialty"}),
    # "service" is out: stemming collapses the noun into the verb, so every
    # mention of a service would read as a repair.
    "repair": frozenset({"fix", "repair", "replace"}),
    "use": frozenset(
        {
            "choose",
            "follow",
            "listen",
            "play",
            "read",
            "rely",
            "subscribe",
            "use",
            "watch",
        }
    ),
    "volunteer": frozenset({"volunteer"}),
}

_QUERY_ACTION_TERM_SURFACES: dict[str, frozenset[str]] = {
    **_ACTION_TERM_SURFACES,
    "recommend": frozenset(
        {
            "recommend",
            "suggest",
        }
    ),
}

_RELATIVE_TERM = normalize_keyword_token("relative")

_RELATION_TERM_SURFACES: dict[str, frozenset[str]] = {
    "friend": frozenset({"colleague", "coworker", "friend", "partner", "roommate"}),
    "relative": frozenset(
        {
            "aunt",
            "brother",
            "cousin",
            "dad",
            "daughter",
            "family",
            "father",
            "mom",
            "mother",
            "nephew",
            "niece",
            "parent",
            "relative",
            "sibling",
            "sister",
            "son",
            "uncle",
        }
    ),
}


_ACTION_TERMS: dict[str, frozenset[str]] = {
    label: normalize_keyword_tokens(terms) for label, terms in _ACTION_TERM_SURFACES.items()
}
_QUERY_ACTION_TERMS: dict[str, frozenset[str]] = {
    label: normalize_keyword_tokens(terms) for label, terms in _QUERY_ACTION_TERM_SURFACES.items()
}
_RELATION_TERMS: dict[str, frozenset[str]] = {
    label: normalize_keyword_tokens(terms) for label, terms in _RELATION_TERM_SURFACES.items()
}
# A word this file actually lists is a vocabulary word, whatever it ends in, so
# the derivational guard must never strip it: "family" is the relative group's
# own entry and only looks like an adverb.
_SENSE_VOCABULARY: frozenset[str] = frozenset(
    term
    for source in (_QUERY_ACTION_TERM_SURFACES, _RELATION_TERM_SURFACES)
    for terms in source.values()
    for term in terms
)


@dataclass(frozen=True)
class FactFrame:
    actions: frozenset[str]
    categories: frozenset[str]
    relations: frozenset[str]
    terms: frozenset[str]
    personal: bool
    span: str


def extract_query_fact_frames(query: str) -> tuple[FactFrame, ...]:
    return _extract_fact_frames(query, query=True)


def extract_evidence_fact_frames(text: str) -> tuple[FactFrame, ...]:
    return _extract_fact_frames(text, query=False)


def score_fact_frame_match(query: str, evidence_text: str) -> float:
    query_frames = extract_query_fact_frames(query)
    return score_fact_frame_match_for_query(query_frames, evidence_text)


def score_fact_frame_match_for_query(
    query_frames: tuple[FactFrame, ...],
    evidence_text: str,
) -> float:
    evidence_frames = extract_evidence_fact_frames(evidence_text)
    if not query_frames or not evidence_frames:
        return 0.0

    return max(
        (
            _score_pair(query_frame, evidence_frame)
            for query_frame in query_frames
            for evidence_frame in evidence_frames
        ),
        default=0.0,
    )


def _extract_fact_frames(text: str, *, query: bool) -> tuple[FactFrame, ...]:
    frames: list[FactFrame] = []
    spans = [span.strip() for span in _SPAN_SPLIT_PATTERN.split(text) if span.strip()]
    if query and len(spans) > 1:
        spans.append(text)

    for span in spans or [text]:
        frame = _frame_from_span(span, query=query)
        if frame is not None:
            frames.append(frame)

    return tuple(_dedupe_frames(frames))


def _frame_from_span(span: str, *, query: bool) -> FactFrame | None:
    span_terms, span_sense_terms = _salient_and_sense_terms(span)
    terms = frozenset(span_terms)
    if not terms:
        return None

    sense_terms = frozenset(span_sense_terms)
    action_source = _QUERY_ACTION_TERMS if query else _ACTION_TERMS
    actions = set(_labels_for_terms(sense_terms, action_source))
    categories: set[str] = set()
    relations = set(_labels_for_terms(sense_terms, _RELATION_TERMS))
    lowered = span.lower()

    if _PREFERENCE_PATTERN.search(span):
        actions.add("preference")
    if _PROFILE_PATTERN.search(span):
        actions.add("profile")
        categories.add("professional_domain")
    service_use_match = _SERVICE_USE_PATTERN.search(span)
    if service_use_match:
        actions.add("use")
        categories.add("service")
        if re.search(r"\b(?:listening|watching|streaming|playing|reading)\b", lowered):
            categories.add("media")
    if query and re.search(r"\b(?:what|which|name)\b[^?]{0,100}\bservice\b", lowered):
        categories.add("service")
        actions.add("use")
    if query and _RELATIVE_TERM in terms:
        relations.add("relative")
    if _RECENCY_PATTERN.search(span):
        relations.add("recency")
    if query and "recommend" in actions and categories & {"professional_domain", "service"}:
        actions.add("profile")

    if not actions and not categories and not relations:
        return None

    return FactFrame(
        actions=frozenset(actions),
        categories=frozenset(categories),
        relations=frozenset(relations),
        terms=terms,
        personal=bool(_FIRST_PERSON_PATTERN.search(span)),
        span=span,
    )


def _score_pair(query_frame: FactFrame, evidence_frame: FactFrame) -> float:
    score = 0.0
    action_overlap = _overlap(query_frame.actions, evidence_frame.actions)
    category_overlap = _overlap(query_frame.categories, evidence_frame.categories)
    relation_overlap = _overlap(query_frame.relations, evidence_frame.relations)
    term_overlap = _overlap(query_frame.terms, evidence_frame.terms)

    if query_frame.actions:
        score += 0.34 * action_overlap
    if query_frame.categories:
        score += 0.36 * category_overlap
    if query_frame.relations:
        score += 0.16 * relation_overlap
    score += 0.18 * term_overlap

    if "recommend" in query_frame.actions and evidence_frame.actions & {
        "preference",
        "profile",
        "use",
    }:
        if query_frame.categories & evidence_frame.categories:
            score = max(score, 0.84)
        elif evidence_frame.personal:
            score = max(score, 0.68)

    if (
        query_frame.categories & {"service"}
        and evidence_frame.categories & {"service"}
        and action_overlap > 0.0
    ):
        requires_media = "media" in query_frame.categories
        if not requires_media or "media" in evidence_frame.categories:
            service_score = 0.9
            if "recency" in query_frame.relations and "recency" in evidence_frame.relations:
                service_score = 0.98
            score = max(score, service_score)

    if (
        query_frame.actions & {"acquire"}
        and evidence_frame.actions & {"acquire"}
        and category_overlap > 0.0
    ):
        score = max(score, 0.92)

    return min(1.0, score)


def _labels_for_terms(
    terms: frozenset[str],
    groups: dict[str, frozenset[str]],
) -> Iterable[str]:
    for label, group_terms in groups.items():
        if terms & group_terms:
            yield label


def _salient_and_sense_terms(text: str) -> tuple[list[str], list[str]]:
    """Both term views of a span, from one traversal.

    Every frame wants the plain terms and the sense terms, and the spans are
    cut from candidate text that runs to fifty thousand characters, so reading
    the span twice to apply one extra filter is work nobody needs. The two
    views dedupe separately, exactly as two passes would: the sense view never
    sees the derived forms, so they cannot claim a slot in it.
    """
    terms: list[str] = []
    sense_terms: list[str] = []
    seen: set[str] = set()
    sense_seen: set[str] = set()
    for raw_token in _TOKEN_PATTERN.findall(text.lower()):
        if raw_token in _STOPWORDS:
            continue
        token = normalize_keyword_token(raw_token)
        if len(token) < 2:
            continue
        if token not in seen:
            seen.add(token)
            terms.append(token)
        if token in sense_seen or is_derivational_form(raw_token, vocabulary=_SENSE_VOCABULARY):
            continue
        sense_seen.add(token)
        sense_terms.append(token)
    return terms, sense_terms


def _overlap(left: frozenset[str], right: frozenset[str]) -> float:
    if not left:
        return 0.0
    return len(left & right) / len(left)


def _dedupe_frames(frames: list[FactFrame]) -> list[FactFrame]:
    deduped: list[FactFrame] = []
    seen: set[tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]] = set()
    for frame in frames:
        key = (frame.actions, frame.categories, frame.relations, frame.terms)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(frame)
    return deduped
