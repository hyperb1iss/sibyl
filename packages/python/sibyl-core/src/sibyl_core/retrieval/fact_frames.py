"""Typed fact frames for query/evidence matching."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

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
    "months",
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
    "using",
    "was",
    "week",
    "weeks",
    "what",
    "when",
    "which",
    "who",
    "with",
    "year",
    "you",
}

_NORMALIZED_TOKEN_ALIASES = {
    "acquired": "acquire",
    "acquiring": "acquire",
    "assembled": "assemble",
    "assembling": "assemble",
    "attended": "attend",
    "attending": "attend",
    "bought": "buy",
    "classes": "class",
    "completed": "complete",
    "finishing": "finish",
    "fixed": "fix",
    "fixing": "fix",
    "got": "get",
    "listened": "listen",
    "listening": "listen",
    "ordered": "order",
    "ordering": "order",
    "participated": "participate",
    "participating": "participate",
    "picked": "pick",
    "played": "play",
    "playing": "play",
    "purchased": "purchase",
    "purchasing": "purchase",
    "read": "read",
    "relying": "rely",
    "researched": "research",
    "researching": "research",
    "serviced": "repair",
    "servicing": "repair",
    "studied": "study",
    "studying": "study",
    "subscribed": "subscribe",
    "using": "use",
    "visited": "visit",
    "visiting": "visit",
    "volunteered": "volunteer",
    "watching": "watch",
}

_ACTION_TERMS: dict[str, frozenset[str]] = {
    "acquire": frozenset(
        {
            "acquire",
            "buy",
            "get",
            "invest",
            "order",
            "pick",
            "purchase",
        }
    ),
    "attend": frozenset(
        {
            "attend",
            "complete",
            "finish",
            "join",
            "participate",
            "present",
            "visit",
            "volunteer",
            "went",
        }
    ),
    "create": frozenset({"build", "compose", "create", "draft", "generate", "make", "write"}),
    "profile": frozenset({"field", "focus", "profession", "research", "role", "specialty"}),
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
}

_QUERY_ACTION_TERMS: dict[str, frozenset[str]] = {
    **_ACTION_TERMS,
    "recommend": frozenset(
        {
            "recommend",
            "suggest",
        }
    ),
}

_CATEGORY_TERMS: dict[str, frozenset[str]] = {
    "appliance": frozenset(
        {
            "appliance",
            "bbq",
            "blender",
            "coffee",
            "cooker",
            "fryer",
            "grill",
            "kettle",
            "kitchen",
            "mixer",
            "oven",
            "processor",
            "toaster",
        }
    ),
    "event": frozenset(
        {
            "anniversary",
            "ceremony",
            "conference",
            "event",
            "exhibition",
            "festival",
            "gala",
            "opening",
            "reunion",
            "workshop",
        }
    ),
    "life_event": frozenset(
        {
            "anniversary",
            "birthday",
            "ceremony",
            "engagement",
            "funeral",
            "graduation",
            "memorial",
            "party",
            "reunion",
            "shower",
            "wedding",
        }
    ),
    "media": frozenset(
        {
            "album",
            "audio",
            "book",
            "documentary",
            "film",
            "movie",
            "music",
            "podcast",
            "series",
            "show",
            "song",
            "stream",
            "video",
        }
    ),
    "professional_domain": frozenset(
        {
            "analysis",
            "conference",
            "domain",
            "field",
            "journal",
            "paper",
            "profession",
            "publication",
            "research",
            "role",
            "specialty",
            "study",
        }
    ),
    "service": frozenset(
        {
            "app",
            "delivery",
            "platform",
            "provider",
            "service",
            "streaming",
            "subscription",
        }
    ),
    "tool": frozenset(
        {
            "drill",
            "driver",
            "equipment",
            "gadget",
            "hardware",
            "kit",
            "machine",
            "saw",
            "tool",
        }
    ),
}

_RELATION_TERMS: dict[str, frozenset[str]] = {
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
    categories = set(_labels_for_terms(terms, _CATEGORY_TERMS))
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
    if query and "life" in terms and "event" in terms:
        categories.add("life_event")
    if query and "relative" in terms:
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
        query_frame.categories & {"life_event"}
        and evidence_frame.categories & {"life_event"}
        and (not query_frame.relations or relation_overlap > 0.0)
    ):
        score = max(score, 0.88)

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
        token = _normalize_token(raw_token)
        if token in _STOPWORDS or token in seen or len(token) < 2:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def _normalize_token(token: str) -> str:
    token = token.strip("'\"")
    if token.endswith("'s"):
        token = token[:-2]
    elif token.endswith("'"):
        token = token[:-1]
    if token in _NORMALIZED_TOKEN_ALIASES:
        return _NORMALIZED_TOKEN_ALIASES[token]
    if len(token) > 4 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 4 and token.endswith(("ches", "shes", "xes", "zes")):
        return token[:-2]
    if len(token) > 4 and token.endswith(("ces", "ses")):
        return token[:-1]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("is", "ous", "ss", "us")):
        return token[:-1]
    return token


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
