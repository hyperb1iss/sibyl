"""Typed fact frames for query/evidence matching."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from sibyl_core.query_anchors import normalize_keyword_token, normalize_keyword_tokens

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
    "more",
    "much",
    "my",
    "name",
    "need",
    "new",
    "on",
    "or",
    "our",
    "out",
    "recent",
    "some",
    "that",
    "the",
    "their",
    "this",
    "today",
    "to",
    "up",
    "user",
    "was",
    "week",
    "what",
    "when",
    "which",
    "who",
    "with",
    "year",
    "you",
}
# Matched both ways: as written, so an entry only silences the word it spells,
# and by stem, so its own plural cannot slip past the entry that lists it.
# "using" is deliberately absent from both, since it carries the use action.
_STOPWORD_STEMS = normalize_keyword_tokens(_STOPWORDS)

# Every group is written in surface English and stemmed at import, so a term
# covers its own inflections. Irregular pasts (bought, got, went) survive
# stemming unchanged and are therefore listed beside the verb they belong to,
# and "service" sits under repair because that is a sense mapping the stemmer
# has no opinion about.
_ACTION_TERMS: dict[str, frozenset[str]] = {
    "acquire": normalize_keyword_tokens(
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
    "attend": normalize_keyword_tokens({"attend", "join", "participate", "visit", "went"}),
    "complete": normalize_keyword_tokens({"complete", "finish"}),
    "create": normalize_keyword_tokens(
        {"build", "compose", "create", "draft", "generate", "make", "write"}
    ),
    "present": normalize_keyword_tokens({"present", "presentation"}),
    "profile": normalize_keyword_tokens(
        {"field", "focus", "profession", "research", "role", "specialty"}
    ),
    "repair": normalize_keyword_tokens({"fix", "repair", "replace", "service"}),
    "use": normalize_keyword_tokens(
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
    "volunteer": normalize_keyword_tokens({"volunteer"}),
}

_QUERY_ACTION_TERMS: dict[str, frozenset[str]] = {
    **_ACTION_TERMS,
    "recommend": normalize_keyword_tokens(
        {
            "recommend",
            "suggest",
        }
    ),
}

_RELATIVE_TERM = normalize_keyword_token("relative")

_RELATION_TERMS: dict[str, frozenset[str]] = {
    "friend": normalize_keyword_tokens({"colleague", "coworker", "friend", "partner", "roommate"}),
    "relative": normalize_keyword_tokens(
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
    terms = frozenset(_salient_terms(span))
    if not terms:
        return None

    action_source = _QUERY_ACTION_TERMS if query else _ACTION_TERMS
    actions = set(_labels_for_terms(terms, action_source))
    categories: set[str] = set()
    relations = set(_labels_for_terms(terms, _RELATION_TERMS))
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


def _salient_terms(text: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw_token in _TOKEN_PATTERN.findall(text.lower()):
        if raw_token in _STOPWORDS:
            continue
        token = normalize_keyword_token(raw_token)
        if token in _STOPWORD_STEMS or token in seen or len(token) < 2:
            continue
        seen.add(token)
        terms.append(token)
    return terms


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
