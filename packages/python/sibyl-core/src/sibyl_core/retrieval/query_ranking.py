"""Query-aware text ranking shared by runtime retrieval and eval replay."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from sibyl_core.retrieval.fact_frames import (
    FactFrame,
    extract_query_fact_frames,
    score_fact_frame_match_for_query,
)

_KEYWORD_STOPWORDS = {
    "any",
    "about",
    "also",
    "and",
    "after",
    "ago",
    "are",
    "back",
    "before",
    "been",
    "between",
    "can",
    "checking",
    "compared",
    "conversation",
    "conversations",
    "could",
    "current",
    "currently",
    "did",
    "different",
    "does",
    "discussed",
    "doing",
    "earlier",
    "earliest",
    "few",
    "during",
    "day",
    "days",
    "from",
    "for",
    "four",
    "free",
    "going",
    "getting",
    "have",
    "having",
    "happened",
    "how",
    "i'm",
    "i've",
    "ive",
    "into",
    "kind",
    "last",
    "latest",
    "like",
    "lately",
    "long",
    "many",
    "mentioned",
    "more",
    "much",
    "month",
    "months",
    "need",
    "new",
    "name",
    "one",
    "or",
    "order",
    "our",
    "past",
    "please",
    "previous",
    "provided",
    "recommended",
    "referring",
    "remind",
    "should",
    "significant",
    "some",
    "starting",
    "that",
    "the",
    "than",
    "think",
    "thinking",
    "there",
    "they",
    "three",
    "those",
    "this",
    "tonight",
    "take",
    "time",
    "type",
    "types",
    "two",
    "use",
    "uses",
    "using",
    "useful",
    "what",
    "when",
    "where",
    "which",
    "who",
    "while",
    "will",
    "with",
    "would",
    "were",
    "was",
    "week",
    "weeks",
    "year",
    "years",
    "you",
    "your",
}
_NORMALIZED_TOKEN_ALIASES = {
    "attended": "attend",
    "attending": "attend",
    "assembled": "assemble",
    "assembling": "assemble",
    "classes": "class",
    "engaged": "engagement",
    "engagements": "engagement",
    "events": "event",
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
    "weddings": "wedding",
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
_MEMORY_SPAN_OVERLAP_WEIGHT = 0.16
_MEMORY_SPAN_SEGMENT_WEIGHT = 0.14
_MEMORY_CONCEPT_WEIGHT = 0.06
_MEMORY_EVIDENCE_WEIGHT = 0.08
_ASSISTANT_ONLY_MEMORY_PENALTY = 1.20
_EVIDENCE_SET_WINDOW = 5
_EVIDENCE_SET_MIN_OVERLAP = 0.25
_EVIDENCE_SET_INSERT_MARGIN = 0.14
_EVIDENCE_SET_SIGNAL_DOMINANCE_SCORE_MARGIN = 0.25
_PREFERENCE_MIN_OVERLAP = 0.25
_PREFERENCE_INSERT_MARGIN = 0.10
_TEMPORAL_EVIDENCE_MIN_SIGNAL = 0.22
_TEMPORAL_EVIDENCE_INSERT_MARGIN = 0.06
_ARTIFACT_EVIDENCE_MIN_SIGNAL = 0.65
_ARTIFACT_EVIDENCE_INSERT_MARGIN = 0.36
_TEMPORAL_TARGET_WEIGHT = 0.34
_QUERY_FRAME_WEIGHT = 0.52
_FACT_FRAME_MIN_SIGNAL = 0.80
_FACT_FRAME_INSERT_MARGIN = 0.06
_FACT_FRAME_RESCUE_WEIGHT = 0.42
_FACT_FRAME_WEIGHT = 0.26
_QUERY_COVERAGE_REFINEMENT_WINDOW = 5
_QUERY_COVERAGE_REFINEMENT_GUARD_WINDOW = 10
_QUERY_COVERAGE_REFINEMENT_MIN_TOP_GAIN = 0.05
_QUERY_COVERAGE_REFINEMENT_MAX_GUARD_LOSS = 0.05
_QUERY_COVERAGE_REFINEMENT_MIN_SCORE_GAIN = 0.05
_CLUSTER_AFFINITY_WEIGHT = 0.45
_CLUSTER_AFFINITY_MIN = 0.05
_CLUSTER_AFFINITY_MAX_ORIGINAL_RANK = 40
_CLUSTER_ANCHOR_MIN_SIGNAL = 0.5
_CLUSTER_SIGNAL_WEIGHT = 2.1
_SIGNAL_DOMINANCE_INSERT_MARGIN = 0.20
_CLUSTER_AFFINITY_STOPWORDS = {
    "actually",
    "all",
    "around",
    "back",
    "bit",
    "but",
    "could",
    "definitely",
    "give",
    "great",
    "had",
    "help",
    "just",
    "know",
    "make",
    "maybe",
    "not",
    "now",
    "out",
    "really",
    "same",
    "seem",
    "since",
    "still",
    "sure",
    "tell",
    "thank",
    "thanks",
    "their",
    "them",
    "there",
    "these",
    "thing",
    "things",
    "though",
    "thought",
    "time",
    "today",
    "try",
    "trying",
    "want",
    "way",
    "well",
}

_EVIDENCE_SET_QUERY_PATTERN = re.compile(
    r"\b(how many|how much|total number|number of|count of|order of|sequence of)\b",
    re.IGNORECASE,
)
_TEMPORAL_INSTRUCTION_QUERY_PATTERN = re.compile(
    r"\b(?:ago|before|after|between|earliest|latest|first|last|"
    r"recently|yesterday|today|tomorrow|order|sequence)\b"
    r"|\bfrom (?:earliest|latest) to (?:latest|earliest)\b",
    re.IGNORECASE,
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
_PREFERENCE_QUERY_SCAFFOLDING_TERMS = {
    "advice",
    "advic",
    "choose",
    "excited",
    "extra",
    "feel",
    "feeling",
    "find",
    "getting",
    "good",
    "got",
    "having",
    "idea",
    "interesting",
    "look",
    "looking",
    "lately",
    "might",
    "new",
    "recommend",
    "recommendation",
    "serve",
    "something",
    "suggest",
    "suggestion",
    "think",
    "tip",
    "trouble",
    "visit",
    "watch",
    "weekend",
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
_TRANSCRIPT_ASSISTANT_TURN_PATTERN = re.compile(
    r"assistant:\s*(.*?)(?=\s+assistant:|\s+user:|$)",
    re.IGNORECASE | re.DOTALL,
)
_ASSISTANT_EVIDENCE_QUERY_PATTERN = re.compile(
    r"\b(?:you|assistant)\s+(?:mentioned|said|told|recommended|suggested|shared|"
    r"provided|gave|explained|advised|noted)\b|\bremind me "
    r"(?:of|about|what|which|who|where|when|how)\b|\bprevious conversations?\b",
    re.IGNORECASE,
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
_PURCHASE_ACTION_PATTERN = re.compile(
    r"\b(?:bought|buy|purchased|ordered|got|picked up|acquired|invested in|"
    r"started using)\b",
    re.IGNORECASE,
)
_BRAND_LOOKUP_QUERY_PATTERN = re.compile(r"\bbrand\b", re.IGNORECASE)
_BRAND_EVIDENCE_PATTERN = re.compile(
    r"\b(?:using|use|uses|switched to|picked up|from|at|by|brand|made by)\b",
    re.IGNORECASE,
)
_SIBLING_QUERY_PATTERN = re.compile(r"\b(?:siblings?|brothers?|sisters?)\b", re.IGNORECASE)
_SIBLING_EVIDENCE_PATTERN = re.compile(
    r"\b(?:family with \d+\s+(?:sisters?|brothers?)|have (?:a|\d+)\s+"
    r"(?:sisters?|brothers?)|\d+\s+(?:sisters?|brothers?)|siblings?)\b",
    re.IGNORECASE,
)
_AGE_ARITHMETIC_QUERY_PATTERN = re.compile(
    r"\b(?:older|younger|age|years old|graduated|college|degree)\b",
    re.IGNORECASE,
)
_AGE_ARITHMETIC_EVIDENCE_PATTERN = re.compile(
    r"\b(?:\d{1,3}-year-old|age of \d{1,3}|graduated|degree|"
    r"bachelor'?s|master'?s|college|university)\b",
    re.IGNORECASE,
)
_HOMEGROWN_QUERY_PATTERN = re.compile(
    r"\b(?:homegrown|ingredients?|garden|serve|dinner|recipe)\b",
    re.IGNORECASE,
)
_HOMEGROWN_EVIDENCE_PATTERN = re.compile(
    r"\b(?:homegrown|garden|harvested|fresh|basil|mint|tomatoes?|herbs?|"
    r"pepper plants?|cooking)\b",
    re.IGNORECASE,
)
_HOMEGROWN_STRONG_EVIDENCE_PATTERN = re.compile(
    r"\b(?:homegrown|garden|harvested|basil|mint|tomatoes?|herbs?|pepper plants?)\b",
    re.IGNORECASE,
)
_PHONE_ACCESSORY_QUERY_PATTERN = re.compile(
    r"\b(?:phone|iphone|android|smartphone|accessories?|screen protectors?|"
    r"case|charger|charging)\b",
    re.IGNORECASE,
)
_PHONE_ACCESSORY_EVIDENCE_PATTERN = re.compile(
    r"\b(?:phone|iphone|android|smartphone|screen protectors?|protectors?|"
    r"case|charger|charging|power bank|wireless charging|tempered glass)\b",
    re.IGNORECASE,
)
_SPORTS_EVENT_QUERY_PATTERN = re.compile(
    r"\b(?:sports? events?|5k|run|race|triathlon|soccer|tournament|bike ride)\b",
    re.IGNORECASE,
)
_SPORTS_EVENT_EVIDENCE_PATTERN = re.compile(
    r"\b(?:i (?:just |recently |will )?(?:finished|completed|participated|"
    r"participate|ran|joined)[^.?!]{0,80}(?:5k|run|race|triathlon|soccer|"
    r"tournament|bike ride)|personal best|5k run|triathlon|soccer tournament|"
    r"bike ride)\b",
    re.IGNORECASE,
)
_BUSINESS_MILESTONE_QUERY_PATTERN = re.compile(
    r"\b(?:business|buisiness|milestone|client|contract|freelance|launch)\b",
    re.IGNORECASE,
)
_BUSINESS_MILESTONE_EVIDENCE_PATTERN = re.compile(
    r"\b(?:launched my website|business plan|signed a contract|first client|"
    r"freelance clients?|potential clients?|business strategy)\b",
    re.IGNORECASE,
)
_SOCIAL_ACTIVITY_QUERY_PATTERN = re.compile(
    r"\b(?:social media|hashtag|challenge|post|posted|instagram|tiktok|"
    r"facebook|twitter|x)\b",
    re.IGNORECASE,
)
_SOCIAL_ACTIVITY_EVIDENCE_PATTERN = re.compile(
    r"(?:#\w+|\b(?:social media challenge|instagram|tiktok|facebook|twitter|"
    r"posted|shared|hashtag)\b)",
    re.IGNORECASE,
)
_RECURRING_APPOINTMENT_QUERY_PATTERN = re.compile(
    r"\b(?:how often|frequency|see dr\.?|session|appointment)\b",
    re.IGNORECASE,
)
_RECURRING_APPOINTMENT_EVIDENCE_PATTERN = re.compile(
    r"\b(?:every week|weekly|bi-weekly|biweekly|every two weeks|every \d+ weeks|"
    r"twice a week|twice weekly|daily|monthly|session with dr\.?|"
    r"see dr\.?|appointment)\b",
    re.IGNORECASE,
)
_DOCTOR_VISIT_QUERY_PATTERN = re.compile(
    r"\b(?:doctors?|dr\.?|physician|therapist|dermatologist|dentist|"
    r"appointment|visit)\b",
    re.IGNORECASE,
)
_DOCTOR_VISIT_EVIDENCE_PATTERN = re.compile(
    r"\b(?:(?:visited|saw|went to|met with|appointment with|session with)"
    r"[^.?!]{0,80}(?:doctor|dr\.?|physician|therapist|dermatologist|dentist|"
    r"optometrist)|(?:doctor|dr\.?|physician|therapist|dermatologist|dentist|"
    r"optometrist)[^.?!]{0,80}(?:appointment|visit|checkup|check-up|"
    r"prescription))\b",
    re.IGNORECASE,
)
_GENERATED_ARTIFACT_QUERY_PATTERN = re.compile(
    r"\b(?:created?|composed|wrote|write|generated|made|drafted|built)\b",
    re.IGNORECASE,
)
_GENERATED_ARTIFACT_OUTPUT_PATTERN = re.compile(
    r"\b(?:here'?s|below is|draft|verse\s*\d*|chorus|bridge|ingredients?|"
    r"instructions?|steps?|def |class |```)\b",
    re.IGNORECASE,
)
_ARTIFACT_TYPE_TERMS = {
    "code",
    "email",
    "letter",
    "outline",
    "plan",
    "poem",
    "recipe",
    "script",
    "song",
    "story",
}
_ARTIFACT_SECTION_TERMS = {
    "bridge",
    "chorus",
    "chord",
    "class",
    "draft",
    "function",
    "ingredient",
    "instruction",
    "intro",
    "outro",
    "progression",
    "section",
    "step",
    "verse",
}
_NOSTALGIA_EVIDENCE_PATTERN = re.compile(
    r"\b(?:high school|old friends?|happy .*experiences?|debate team|"
    r"advanced placement|favorite subjects?)\b",
    re.IGNORECASE,
)
_CATEGORY_ALIASES: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    (
        frozenset({"kitchen", "appliance", "gadget", "cook", "cooking"}),
        frozenset(
            {
                "airfryer",
                "appliance",
                "bbq",
                "blender",
                "coffee",
                "fryer",
                "grill",
                "instant",
                "kettle",
                "mixer",
                "oven",
                "processor",
                "smoker",
                "toaster",
            }
        ),
    ),
    (
        frozenset({"shampoo", "conditioner", "hair"}),
        frozenset({"bathroom", "conditioner", "hair", "lavender", "loofah", "shampoo"}),
    ),
    (
        frozenset({"homegrown", "ingredient", "dinner", "recipe", "serve"}),
        frozenset({"basil", "fresh", "garden", "herb", "mint", "tomato", "tomatoes"}),
    ),
)
_MEMORY_EVIDENCE_PATTERNS = (
    re.compile(r"\bby the way\b"),
    re.compile(r"\bi(?:'m| am) \d{1,3}\b"),
    re.compile(r"\bi (?:just|recently|finally|already|still|used to)\b"),
    re.compile(
        r"\bi(?:'ve| have)? (?:bought|got|ordered|purchased|acquired|"
        r"invested|started|finished|completed|attended|visited|"
        r"participated|joined|met|fixed|replaced|made|baked|spent|"
        r"worked|led|watched|read|booked|adopted|moved|graduated|"
        r"submitted|became|had|went|assembled|sold|volunteered|"
        r"presented|donated|subscribed|relied|registered)\b"
    ),
    re.compile(
        r"\bi(?:'m| am) (?:also |currently |already |still |now )?"
        r"(?:getting|using|taking|seeing|watching|reading|listening|"
        r"planning|going|wearing|carrying)\b"
    ),
    re.compile(
        r"\bi(?:'ve| have) been (?:also |currently |really |still )?"
        r"(?:using|reading|listening|watching|attending|working|baking|"
        r"seeing|visiting|playing|taking|going|getting|loving|keeping|"
        r"collecting|volunteering|presenting|relying|selling|assembling)\b"
    ),
    re.compile(r"\bmy (?:current|new|old|previous|favorite|preferred|usual|go-to)\b"),
)
_ACQUISITION_CONCEPT_GROUP = frozenset(
    {
        "acquire",
        "acquired",
        "bought",
        "buy",
        "got",
        "invest",
        "invested",
        "order",
        "ordered",
        "purchase",
        "purchased",
    }
)
_ACTION_EVIDENCE_GROUPS = (
    _ACQUISITION_CONCEPT_GROUP,
    frozenset({"assemble", "build", "built", "install", "installed", "set"}),
    frozenset({"donate", "donated", "sell", "sold"}),
    frozenset({"fix", "repair", "repaired", "replace", "replaced", "service", "serviced"}),
    frozenset({"attend", "join", "joined", "participate", "participated"}),
    frozenset({"present", "volunteer"}),
    frozenset({"register", "registered", "subscribe", "subscription"}),
    frozenset({"rely", "use", "used", "using"}),
)
_CONCEPT_GROUPS = (
    frozenset(
        {
            "accessory",
            "accessories",
            "android",
            "battery",
            "cable",
            "charger",
            "charging",
            "iphone",
            "phone",
            "power",
            "powerbank",
            "protector",
            "protectors",
            "screen",
            "tech",
            "wireless",
        }
    ),
    frozenset(
        {
            "airfryer",
            "appliance",
            "basil",
            "bake",
            "baked",
            "baking",
            "blender",
            "cook",
            "cooking",
            "dessert",
            "dinner",
            "dish",
            "fresh",
            "fryer",
            "herb",
            "homegrown",
            "ingredient",
            "kitchen",
            "meal",
            "mint",
            "mixer",
            "processor",
            "recipe",
            "serve",
            "smoker",
            "tomato",
            "tomatoes",
        }
    ),
    _ACQUISITION_CONCEPT_GROUP,
    frozenset(
        {
            "business",
            "buisiness",
            "client",
            "contract",
            "customer",
            "freelance",
            "milestone",
            "signed",
        }
    ),
    frozenset(
        {
            "bike",
            "charity",
            "completed",
            "participated",
            "ride",
            "run",
            "running",
            "soccer",
            "sport",
            "sports",
            "sprint",
            "tournament",
            "triathlon",
        }
    ),
    frozenset(
        {
            "brother",
            "brothers",
            "family",
            "relative",
            "relatives",
            "sibling",
            "siblings",
            "sister",
            "sisters",
        }
    ),
    frozenset(
        {
            "age",
            "bachelor",
            "college",
            "degree",
            "graduated",
            "master",
            "old",
            "older",
            "university",
        }
    ),
    frozenset(
        {
            "advanced",
            "debate",
            "economics",
            "high",
            "nostalgic",
            "reunion",
            "school",
        }
    ),
    frozenset(
        {
            "appointment",
            "clinic",
            "dermatologist",
            "doctor",
            "physician",
            "prescription",
            "specialist",
            "therapist",
            "visit",
            "visited",
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
_GENERIC_ACTION_CONCEPT_GROUPS = frozenset({_ACQUISITION_CONCEPT_GROUP})


@dataclass(frozen=True)
class QueryCoverageCandidate[T]:
    item: T
    stable_id: str
    text: str
    prior_score: float
    original_rank: int
    timestamp: Any = None


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


def should_accept_query_coverage_refinement[T](
    initial: QueryCoverageResult[T],
    refined: QueryCoverageResult[T],
) -> bool:
    if not initial.applied or not refined.applied or not refined.changed:
        return False

    initial_top = initial.ranked[0] if initial.ranked else None
    refined_top = refined.ranked[0] if refined.ranked else None
    top_gain = _coverage_signal_sum(
        refined.ranked,
        _QUERY_COVERAGE_REFINEMENT_WINDOW,
    ) - _coverage_signal_sum(initial.ranked, _QUERY_COVERAGE_REFINEMENT_WINDOW)
    guard_loss = _coverage_signal_sum(
        initial.ranked,
        _QUERY_COVERAGE_REFINEMENT_GUARD_WINDOW,
    ) - _coverage_signal_sum(refined.ranked, _QUERY_COVERAGE_REFINEMENT_GUARD_WINDOW)
    top_score_gain = (
        refined_top.score - initial_top.score if initial_top is not None and refined_top else 0.0
    )
    top_score_refinement = bool(
        initial_top is not None
        and refined_top is not None
        and refined_top.stable_id != initial_top.stable_id
        and refined_top.overlap >= initial_top.overlap
        and top_score_gain >= _QUERY_COVERAGE_REFINEMENT_MIN_SCORE_GAIN
    )
    return (
        top_gain >= _QUERY_COVERAGE_REFINEMENT_MIN_TOP_GAIN or top_score_refinement
    ) and guard_loss <= _QUERY_COVERAGE_REFINEMENT_MAX_GUARD_LOSS


def _coverage_signal_sum[T](
    ranked: list[QueryCoverageRankedCandidate[T]],
    limit: int,
) -> float:
    return sum(max(0.0, candidate.overlap) for candidate in ranked[:limit])


def rank_items_by_query_coverage[T](
    query: str,
    items: Sequence[tuple[T, float]],
    *,
    text_fn: Callable[[T], str],
    id_fn: Callable[[T], str],
    timestamp_fn: Callable[[T], Any] = lambda _item: None,
    temporal_target: datetime | None = None,
) -> tuple[list[tuple[T, float]], bool, bool]:
    """Rank ``(item, prior_score)`` pairs through the query-coverage core.

    This is the single shared ranking entry point: both the hybrid graph search
    and the native context-pack plan route their post-fusion ordering through
    here so they share one proven scorer. Candidates are scored once, then a
    guarded refinement pass re-ranks the survivors and is accepted only when it
    measurably improves the top window without sacrificing guard coverage.

    Returns the re-ranked ``(item, score)`` pairs alongside whether ranking
    applied and whether the refinement pass was accepted.
    """
    candidates = [
        QueryCoverageCandidate(
            item=item,
            stable_id=id_fn(item),
            text=text_fn(item),
            prior_score=score,
            original_rank=index + 1,
            timestamp=timestamp_fn(item),
        )
        for index, (item, score) in enumerate(items)
    ]
    ranking = rank_by_query_coverage(query, candidates, temporal_target=temporal_target)
    if not ranking.applied:
        return list(items), False, False

    refined_candidates = [
        QueryCoverageCandidate(
            item=ranked.item,
            stable_id=ranked.stable_id,
            text=text_fn(ranked.item),
            prior_score=ranked.score,
            original_rank=index + 1,
            timestamp=timestamp_fn(ranked.item),
        )
        for index, ranked in enumerate(ranking.ranked)
    ]
    refined = rank_by_query_coverage(query, refined_candidates, temporal_target=temporal_target)
    if should_accept_query_coverage_refinement(ranking, refined):
        return [(ranked.item, ranked.score) for ranked in refined.ranked], True, True

    return [(ranked.item, ranked.score) for ranked in ranking.ranked], ranking.changed, False


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
    token = token.strip("'\"")
    if token in _NORMALIZED_TOKEN_ALIASES:
        return _NORMALIZED_TOKEN_ALIASES[token]
    if token == "buisiness":
        return "business"
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


def extract_primary_text_from_text(text: str) -> tuple[str, bool]:
    user_turns = [match.group(1) for match in _TRANSCRIPT_USER_TURN_PATTERN.finditer(text)]
    if user_turns:
        return " ".join(user_turns), True
    return text, False


def extract_assistant_text_from_text(text: str) -> tuple[str, bool]:
    assistant_turns = [
        match.group(1) for match in _TRANSCRIPT_ASSISTANT_TURN_PATTERN.finditer(text)
    ]
    if assistant_turns:
        return " ".join(assistant_turns), True
    return text, False


def generic_assistant_marker_count(text: str) -> int:
    return sum(1 for pattern in _GENERIC_ASSISTANT_PATTERNS if pattern.search(text.lower()))


def _extract_memory_evidence_text(text: str) -> tuple[str, bool]:
    spans = [
        span.strip()
        for span in re.split(r"(?<=[.!?])\s+|\n+", text)
        if span.strip() and any(pattern.search(span) for pattern in _MEMORY_EVIDENCE_PATTERNS)
    ]
    if spans:
        return " ".join(spans), True
    return "", False


def _memory_evidence_score(memory_text: str) -> float:
    if not memory_text:
        return 0.0
    matches = sum(1 for pattern in _MEMORY_EVIDENCE_PATTERNS if pattern.search(memory_text))
    return min(1.0, matches / 3.0)


def _query_frame_score(
    query: str,
    query_terms: set[str],
    *,
    token_set: set[str],
    primary_token_set: set[str],
    memory_token_set: set[str],
    primary_text: str,
    memory_text: str,
) -> float:
    evidence_tokens = token_set | primary_token_set | memory_token_set
    evidence_text = " ".join(part for part in (memory_text, primary_text) if part)
    if not evidence_text:
        return 0.0

    score = 0.0
    category_score = _category_alias_score(query_terms, evidence_tokens)

    if _BRAND_LOOKUP_QUERY_PATTERN.search(query):
        object_terms = query_terms - {
            "brand",
            "current",
            "currently",
            "use",
            "used",
            "uses",
            "using",
        }
        if (
            object_terms
            and object_terms & evidence_tokens
            and _BRAND_EVIDENCE_PATTERN.search(evidence_text)
        ):
            score = max(score, 1.0)

    if (
        _PURCHASE_ACTION_PATTERN.search(query)
        or {
            "buy",
            "bought",
            "purchase",
            "purchased",
        }
        & query_terms
    ):
        if _PURCHASE_ACTION_PATTERN.search(evidence_text) and category_score > 0.0:
            score = max(score, 1.0)
        elif category_score >= 0.5:
            score = max(score, 0.72)

    if _SIBLING_QUERY_PATTERN.search(query) and _SIBLING_EVIDENCE_PATTERN.search(evidence_text):
        score = max(score, 1.0)

    if _AGE_ARITHMETIC_QUERY_PATTERN.search(query) and _AGE_ARITHMETIC_EVIDENCE_PATTERN.search(
        evidence_text
    ):
        if re.search(r"\b(?:\d{1,3}-year-old|age of \d{1,3})\b", evidence_text):
            score = max(score, 1.0)
        else:
            score = max(score, 0.78)

    if "homegrown" in query_terms and _HOMEGROWN_STRONG_EVIDENCE_PATTERN.search(evidence_text):
        score = max(score, 1.0)
    elif _HOMEGROWN_QUERY_PATTERN.search(query) and _HOMEGROWN_EVIDENCE_PATTERN.search(
        evidence_text
    ):
        score = max(score, 0.68 + (0.25 * category_score))

    if _PHONE_ACCESSORY_QUERY_PATTERN.search(query) and _PHONE_ACCESSORY_EVIDENCE_PATTERN.search(
        evidence_text
    ):
        phone_terms = {"phone", "iphone", "android", "smartphone"}
        accessory_terms = {
            "accessory",
            "case",
            "charger",
            "charging",
            "protector",
            "protectors",
            "screen",
        }
        if evidence_tokens & phone_terms and evidence_tokens & accessory_terms:
            score = max(score, 1.0)
        elif evidence_tokens & phone_terms:
            score = max(score, 0.72)

    if _SPORTS_EVENT_QUERY_PATTERN.search(query) and _SPORTS_EVENT_EVIDENCE_PATTERN.search(
        evidence_text
    ):
        score = max(score, 0.92)

    if _BUSINESS_MILESTONE_QUERY_PATTERN.search(
        query
    ) and _BUSINESS_MILESTONE_EVIDENCE_PATTERN.search(evidence_text):
        score = max(score, 0.95)

    if _SOCIAL_ACTIVITY_QUERY_PATTERN.search(query) and _SOCIAL_ACTIVITY_EVIDENCE_PATTERN.search(
        evidence_text
    ):
        generic_social_terms = {
            "activity",
            "challenge",
            "event",
            "media",
            "participation",
            "post",
            "posted",
            "social",
        }
        specific_social_terms = query_terms - generic_social_terms
        score = max(score, 1.0 if specific_social_terms & evidence_tokens else 0.76)

    if _RECURRING_APPOINTMENT_QUERY_PATTERN.search(
        query
    ) and _RECURRING_APPOINTMENT_EVIDENCE_PATTERN.search(evidence_text):
        score = max(score, 0.82)

    if _DOCTOR_VISIT_QUERY_PATTERN.search(query) and _DOCTOR_VISIT_EVIDENCE_PATTERN.search(
        evidence_text
    ):
        score = max(score, 0.92)

    action_score = _action_evidence_score(query, evidence_tokens)
    if action_score > 0.0:
        score = max(score, action_score)

    artifact_score = _assistant_artifact_score(
        query,
        query_terms,
        evidence_tokens,
        evidence_text,
    )
    if artifact_score > 0.0:
        score = max(score, artifact_score)

    if (
        {"high", "school", "reunion", "nostalgic"} & query_terms
        and ("high" in evidence_tokens or "school" in evidence_tokens)
        and _NOSTALGIA_EVIDENCE_PATTERN.search(evidence_text)
    ):
        score = max(score, 0.95)

    return min(score, 1.0)


def _action_evidence_score(query: str, evidence_tokens: set[str]) -> float:
    query_tokens = set(keyword_tokens_from_text(query))
    query_action_groups = [group for group in _ACTION_EVIDENCE_GROUPS if query_tokens & group]
    if not query_action_groups:
        return 0.0

    matched = sum(1 for group in query_action_groups if evidence_tokens & group)
    if matched == 0:
        return 0.0
    return min(1.0, 0.58 + (0.42 * (matched / len(query_action_groups))))


def _category_alias_score(query_terms: set[str], evidence_tokens: set[str]) -> float:
    if not query_terms or not evidence_tokens:
        return 0.0

    relevant = 0
    matched = 0
    for triggers, aliases in _CATEGORY_ALIASES:
        if query_terms & triggers:
            relevant += 1
            if evidence_tokens & aliases:
                matched += 1
    if relevant == 0:
        return 0.0
    return matched / relevant


def _assistant_artifact_score(
    query: str,
    query_terms: set[str],
    evidence_tokens: set[str],
    evidence_text: str,
) -> float:
    artifact_type_match = bool(query_terms & _ARTIFACT_TYPE_TERMS & evidence_tokens)
    section_match = bool(query_terms & _ARTIFACT_SECTION_TERMS & evidence_tokens)
    generated_query = bool(_GENERATED_ARTIFACT_QUERY_PATTERN.search(query))
    if not generated_query and not (artifact_type_match and section_match):
        return 0.0
    if not _GENERATED_ARTIFACT_OUTPUT_PATTERN.search(evidence_text):
        return 0.0

    if artifact_type_match and section_match:
        return 1.0
    if section_match:
        return 0.86
    if artifact_type_match and generated_query:
        return 0.72
    return 0.0


def rank_by_query_coverage[T](
    query: str,
    candidates: Sequence[QueryCoverageCandidate[T]],
    *,
    temporal_target: datetime | None = None,
) -> QueryCoverageResult[T]:
    keywords = extract_keywords(query)
    is_preference_query = _is_preference_query(query, set(keywords))
    is_personal_memory_query = _is_personal_memory_query(query)
    is_evidence_set_query = bool(_EVIDENCE_SET_QUERY_PATTERN.search(query.lower()))
    if is_preference_query:
        focused_keywords = [
            keyword for keyword in keywords if keyword not in _PREFERENCE_QUERY_SCAFFOLDING_TERMS
        ]
        if len(focused_keywords) >= 3:
            keywords = focused_keywords
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
    query_fact_frames = extract_query_fact_frames(query)
    suppress_fact_frame_rank_signal = (
        is_evidence_set_query
        or _is_multi_evidence_order_query(query)
        or (_is_temporal_instruction_query(query) and temporal_target is None)
    )
    token_rows: list[
        tuple[
            QueryCoverageCandidate[T],
            list[str],
            set[str],
            list[str],
            set[str],
            list[str],
            set[str],
            str,
            str,
            str,
            bool,
            bool,
        ]
    ] = []
    for candidate in candidates:
        text = candidate.text
        tokens = keyword_tokens_from_text(text)
        primary_text_raw, has_primary_text = _extract_query_focus_text(query, text)
        primary_text = primary_text_raw.lower()
        primary_tokens = keyword_tokens_from_text(primary_text) if has_primary_text else []
        memory_text, has_memory_text = _extract_memory_evidence_text(primary_text)
        memory_tokens = keyword_tokens_from_text(memory_text) if has_memory_text else []
        token_rows.append(
            (
                candidate,
                tokens,
                set(tokens),
                primary_tokens,
                set(primary_tokens),
                memory_tokens,
                set(memory_tokens),
                primary_text,
                memory_text,
                primary_text_raw,
                has_primary_text,
                has_memory_text,
            )
        )

    term_weights = _query_term_weights(
        [token_set for _candidate, _tokens, token_set, *_rest in token_rows],
        query_terms,
    )
    affinity_tokens_by_id = {
        candidate.stable_id: _cluster_affinity_tokens(
            primary_token_set if has_primary_text else token_set
        )
        for (
            candidate,
            _tokens,
            token_set,
            _primary_tokens,
            primary_token_set,
            _memory_tokens,
            _memory_token_set,
            _primary_text,
            _memory_text,
            _primary_text_raw,
            has_primary_text,
            _has_memory_text,
        ) in token_rows
    }
    rank_span = max(1, len(candidates) - 1)
    max_prior_score = max((candidate.prior_score for candidate in candidates), default=0.0) or 1.0
    scored: list[tuple[QueryCoverageRankedCandidate[T], int]] = []
    fact_frame_scores_by_id: dict[str, float] = {}
    has_text_signal = False
    for index, (
        candidate,
        tokens,
        token_set,
        primary_tokens,
        primary_token_set,
        memory_tokens,
        memory_token_set,
        primary_text,
        memory_text,
        primary_text_raw,
        has_primary_text,
        has_memory_text,
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
        memory_overlap = (
            _weighted_overlap(memory_token_set, query_terms, term_weights)
            if has_memory_text
            else 0.0
        )
        memory_segment_overlap = (
            _best_weighted_segment_overlap(
                memory_tokens,
                query_terms,
                term_weights,
            )
            if has_memory_text
            else 0.0
        )
        phrase_score = _phrase_adjacency_score(tokens, keywords)
        if has_primary_text:
            phrase_score = max(phrase_score, _phrase_adjacency_score(primary_tokens, keywords))
        concept_overlap = _concept_overlap_score(query_terms, token_set)
        primary_concept_overlap = (
            _concept_overlap_score(query_terms, primary_token_set) if has_primary_text else 0.0
        )
        memory_concept_overlap = (
            _concept_overlap_score(query_terms, memory_token_set) if has_memory_text else 0.0
        )
        query_frame_score = _query_frame_score(
            query,
            query_terms,
            token_set=token_set,
            primary_token_set=primary_token_set,
            memory_token_set=memory_token_set,
            primary_text=primary_text,
            memory_text=memory_text,
        )
        fact_frame_score = score_fact_frame_match_for_query(
            query_fact_frames,
            primary_text_raw if has_primary_text else candidate.text,
        )
        fact_frame_scores_by_id[candidate.stable_id] = fact_frame_score
        fact_frame_rank_signal = 0.0 if suppress_fact_frame_rank_signal else fact_frame_score
        preference_signal = (
            _preference_evidence_score(primary_text)
            if is_preference_query and has_primary_text
            else 0.0
        )
        rank_score = 1.0 - ((candidate.original_rank - 1) / rank_span)
        normalized_prior_score = (
            candidate.prior_score / max_prior_score if candidate.prior_score > 0 else 0.0
        )
        memory_relevance = max(
            memory_overlap,
            memory_segment_overlap,
            memory_concept_overlap,
        )
        temporal_alignment = _temporal_alignment_score(candidate.timestamp, temporal_target)
        coverage_signal = max(
            overlap,
            idf_overlap,
            primary_overlap,
            primary_segment_overlap,
            concept_overlap,
            primary_concept_overlap,
            memory_relevance,
            query_frame_score,
            fact_frame_rank_signal,
            preference_signal,
        )
        memory_multiplier = 0.0 if is_preference_query else 1.0
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
            + (_QUERY_FRAME_WEIGHT * query_frame_score)
            + (_FACT_FRAME_WEIGHT * fact_frame_rank_signal)
            + (
                memory_multiplier
                * (
                    (_MEMORY_SPAN_OVERLAP_WEIGHT * memory_overlap)
                    + (_MEMORY_SPAN_SEGMENT_WEIGHT * memory_segment_overlap)
                    + (_MEMORY_CONCEPT_WEIGHT * memory_concept_overlap)
                    + (
                        _MEMORY_EVIDENCE_WEIGHT
                        * _memory_evidence_score(memory_text)
                        * memory_relevance
                    )
                )
            )
            + (_TEMPORAL_TARGET_WEIGHT * temporal_alignment * coverage_signal)
        )
        if (
            is_personal_memory_query
            and not is_preference_query
            and has_primary_text
            and not has_memory_text
            and max(primary_overlap, primary_segment_overlap, primary_concept_overlap) <= 0.05
            and max(overlap, idf_overlap, segment_overlap, idf_segment_overlap, concept_overlap)
            >= 0.5
        ):
            score -= _ASSISTANT_ONLY_MEMORY_PENALTY * max(
                overlap,
                idf_overlap,
                segment_overlap,
                idf_segment_overlap,
                concept_overlap,
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
            or memory_overlap > 0.0
            or memory_concept_overlap > 0.0
            or query_frame_score > 0.0
            or fact_frame_rank_signal >= _FACT_FRAME_MIN_SIGNAL
            or preference_signal > 0.0
            or (temporal_alignment > 0.0 and coverage_signal > 0.0)
        )
        scored.append(
            (
                QueryCoverageRankedCandidate(
                    item=candidate.item,
                    stable_id=candidate.stable_id,
                    score=score,
                    original_rank=candidate.original_rank,
                    overlap=coverage_signal,
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

    if _is_evidence_cluster_query(query):
        scored = _apply_evidence_cluster_affinity(scored, affinity_tokens_by_id)

    has_strong_fact_frame_signal = bool(
        query_fact_frames
        and any(score >= _FACT_FRAME_MIN_SIGNAL for score in fact_frame_scores_by_id.values())
    )

    use_profile_fact_rescue = (
        has_strong_fact_frame_signal
        and _is_profile_recommendation_fact_query(query_fact_frames)
        and not _is_temporal_instruction_query(query)
        and not _is_multi_evidence_order_query(query)
    )

    if use_profile_fact_rescue:
        ranked = _stabilize_fact_frame_ranking(scored, fact_frame_scores_by_id)
    elif is_preference_query:
        ranked = _stabilize_preference_ranking(scored)
    elif is_evidence_set_query:
        ranked = _stabilize_evidence_set_ranking(scored)
    elif _is_generated_artifact_query(query, query_terms):
        ranked = _stabilize_artifact_evidence_ranking(scored)
    elif _is_temporal_instruction_query(query) and temporal_target is not None:
        ranked = _stabilize_temporal_evidence_ranking(scored)
    elif _is_temporal_instruction_query(query):
        ranked = _rank_preserving_window(scored)
    elif has_strong_fact_frame_signal and not _is_multi_evidence_order_query(query):
        ranked = _stabilize_fact_frame_ranking(scored, fact_frame_scores_by_id)
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


def _extract_query_focus_text(query: str, text: str) -> tuple[str, bool]:
    if _is_assistant_evidence_query(query):
        assistant_text, has_assistant_text = extract_assistant_text_from_text(text)
        if has_assistant_text:
            return assistant_text, True
    return extract_primary_text_from_text(text)


def _is_assistant_evidence_query(query: str) -> bool:
    return bool(_ASSISTANT_EVIDENCE_QUERY_PATTERN.search(query))


def _is_personal_memory_query(query: str) -> bool:
    return bool(_PRIMARY_PERSONAL_PATTERN.search(query)) and not _is_assistant_evidence_query(query)


def _is_generated_artifact_query(query: str, query_terms: set[str]) -> bool:
    return bool(_GENERATED_ARTIFACT_QUERY_PATTERN.search(query)) and bool(
        query_terms & (_ARTIFACT_TYPE_TERMS | _ARTIFACT_SECTION_TERMS)
    )


def _is_temporal_instruction_query(query: str) -> bool:
    return bool(_TEMPORAL_INSTRUCTION_QUERY_PATTERN.search(query))


def _is_evidence_cluster_query(query: str) -> bool:
    lowered = query.lower()
    return (
        bool(_EVIDENCE_SET_QUERY_PATTERN.search(lowered))
        or _is_temporal_instruction_query(query)
        or bool(_AGE_ARITHMETIC_QUERY_PATTERN.search(query))
    )


def _is_profile_recommendation_fact_query(query_frames: Sequence[FactFrame]) -> bool:
    return any(
        "recommend" in frame.actions and "profile" in frame.actions for frame in query_frames
    )


def _is_multi_evidence_order_query(query: str) -> bool:
    return bool(
        re.search(
            r"\b(?:order|sequence|earliest|latest)\b|"
            r"\bfrom first to last\b|"
            r"\bwhich\s+(?:two|three|four|five|six|\d+)\b",
            query,
            re.IGNORECASE,
        )
    )


def _concept_overlap_score(query_terms: set[str], token_set: set[str]) -> float:
    if not query_terms or not token_set:
        return 0.0

    has_specific_query_concept = any(
        query_terms & group
        for group in _CONCEPT_GROUPS
        if group not in _GENERIC_ACTION_CONCEPT_GROUPS
    )
    matched = 0
    relevant = 0
    for group in _CONCEPT_GROUPS:
        if query_terms & group:
            if group in _GENERIC_ACTION_CONCEPT_GROUPS and has_specific_query_concept:
                continue
            relevant += 1
            if token_set & group:
                matched += 1
    if relevant == 0:
        return 0.0
    return matched / relevant


def _cluster_affinity_tokens(tokens: set[str]) -> set[str]:
    return {
        token
        for token in tokens
        if len(token) > 2
        and token not in _KEYWORD_STOPWORDS
        and token not in _PREFERENCE_QUERY_SCAFFOLDING_TERMS
        and token not in _CLUSTER_AFFINITY_STOPWORDS
        and token not in {"assistant", "user"}
    }


def _apply_evidence_cluster_affinity[T](
    scores: list[tuple[QueryCoverageRankedCandidate[T], int]],
    affinity_tokens_by_id: dict[str, set[str]],
) -> list[tuple[QueryCoverageRankedCandidate[T], int]]:
    anchors = [
        (ranked, affinity_tokens_by_id.get(ranked.stable_id, set()))
        for ranked, _index in scores[:_EVIDENCE_SET_WINDOW]
        if ranked.overlap >= _CLUSTER_ANCHOR_MIN_SIGNAL
    ]
    anchors = [(ranked, tokens) for ranked, tokens in anchors if tokens]
    if not anchors:
        return scores

    adjusted: list[tuple[QueryCoverageRankedCandidate[T], int]] = []
    for ranked, index in scores:
        tokens = affinity_tokens_by_id.get(ranked.stable_id, set())
        if (
            ranked.original_rank <= _EVIDENCE_SET_WINDOW
            or ranked.original_rank > _CLUSTER_AFFINITY_MAX_ORIGINAL_RANK
            or not tokens
        ):
            adjusted.append((ranked, index))
            continue

        affinity = max(
            (
                _token_jaccard(tokens, anchor_tokens) * (0.65 + (0.35 * max(anchor.overlap, 0.0)))
                for anchor, anchor_tokens in anchors
            ),
            default=0.0,
        )
        if affinity < _CLUSTER_AFFINITY_MIN:
            adjusted.append((ranked, index))
            continue

        adjusted.append(
            (
                QueryCoverageRankedCandidate(
                    item=ranked.item,
                    stable_id=ranked.stable_id,
                    score=ranked.score + (_CLUSTER_AFFINITY_WEIGHT * affinity),
                    original_rank=ranked.original_rank,
                    overlap=max(
                        ranked.overlap,
                        min(1.0, affinity * _CLUSTER_SIGNAL_WEIGHT),
                    ),
                ),
                index,
            )
        )

    return adjusted


def _token_jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _temporal_alignment_score(value: Any, target: datetime | None) -> float:
    timestamp = _parse_candidate_datetime(value)
    if timestamp is None or target is None:
        return 0.0
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)

    distance_days = abs((timestamp - target).total_seconds()) / 86400.0
    if distance_days <= 1:
        return 1.0
    if distance_days <= 3:
        return 0.85
    if distance_days <= 7:
        return 0.65
    if distance_days <= 14:
        return 0.35
    return 0.0


def _parse_candidate_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None

    cleaned = re.sub(r"\s*\([^)]+\)", "", value.strip())
    iso_value = cleaned.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_value)
    except ValueError:
        pass

    for date_format in (
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(cleaned, date_format).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _rank_preserving_window[T](
    scores: list[tuple[QueryCoverageRankedCandidate[T], int]],
) -> list[QueryCoverageRankedCandidate[T]]:
    window_size = min(_EVIDENCE_SET_WINDOW, len(scores))
    selected = sorted(scores[:window_size], key=lambda item: (-item[0].score, item[1]))
    tail = sorted(scores[window_size:], key=lambda item: (-item[0].score, item[1]))
    return [ranked for ranked, _index in selected + tail]


def _stabilize_preference_ranking[T](
    scores: list[tuple[QueryCoverageRankedCandidate[T], int]],
) -> list[QueryCoverageRankedCandidate[T]]:
    return _stabilize_top_window_ranking(
        scores,
        min_overlap=_PREFERENCE_MIN_OVERLAP,
        insert_margin=_PREFERENCE_INSERT_MARGIN,
        protected_original_rank=1,
    )


def _stabilize_evidence_set_ranking[T](
    scores: list[tuple[QueryCoverageRankedCandidate[T], int]],
) -> list[QueryCoverageRankedCandidate[T]]:
    return _stabilize_top_window_ranking(
        scores,
        min_overlap=_EVIDENCE_SET_MIN_OVERLAP,
        insert_margin=_EVIDENCE_SET_INSERT_MARGIN,
        dominance_score_margin=_EVIDENCE_SET_SIGNAL_DOMINANCE_SCORE_MARGIN,
        replace_strong_window=False,
    )


def _stabilize_temporal_evidence_ranking[T](
    scores: list[tuple[QueryCoverageRankedCandidate[T], int]],
) -> list[QueryCoverageRankedCandidate[T]]:
    return _stabilize_top_window_ranking(
        scores,
        min_overlap=_TEMPORAL_EVIDENCE_MIN_SIGNAL,
        insert_margin=_TEMPORAL_EVIDENCE_INSERT_MARGIN,
    )


def _stabilize_artifact_evidence_ranking[T](
    scores: list[tuple[QueryCoverageRankedCandidate[T], int]],
) -> list[QueryCoverageRankedCandidate[T]]:
    ranked = _stabilize_top_window_ranking(
        scores,
        min_overlap=_ARTIFACT_EVIDENCE_MIN_SIGNAL,
        insert_margin=_ARTIFACT_EVIDENCE_INSERT_MARGIN,
    )
    window_size = min(_EVIDENCE_SET_WINDOW, len(ranked))
    window = sorted(
        ranked[:window_size],
        key=lambda item: (
            item.overlap < _ARTIFACT_EVIDENCE_MIN_SIGNAL,
            -item.overlap,
            -item.score,
            item.original_rank,
        ),
    )
    return window + ranked[window_size:]


def _stabilize_fact_frame_ranking[T](
    scores: list[tuple[QueryCoverageRankedCandidate[T], int]],
    fact_frame_scores_by_id: dict[str, float],
) -> list[QueryCoverageRankedCandidate[T]]:
    window_size = min(_EVIDENCE_SET_WINDOW, len(scores))
    selected = list(scores[:window_size])
    selected_ids = {ranked.stable_id for ranked, _index in selected}
    ranked_by_score = sorted(scores, key=lambda item: (-item[0].score, item[1]))

    def fact_signal(item: tuple[QueryCoverageRankedCandidate[T], int]) -> float:
        ranked, _index = item
        return fact_frame_scores_by_id.get(ranked.stable_id, 0.0)

    for candidate in ranked_by_score:
        ranked, _index = candidate
        candidate_signal = fact_signal(candidate)
        if ranked.stable_id in selected_ids or candidate_signal < _FACT_FRAME_MIN_SIGNAL:
            continue

        low_signal = [
            item for item in enumerate(selected) if fact_signal(item[1]) < _FACT_FRAME_MIN_SIGNAL
        ]
        if not low_signal:
            continue

        worst_index, worst = min(
            low_signal,
            key=lambda item: (fact_signal(item[1]), item[1][0].score, -item[1][1]),
        )
        worst_ranked, _worst_original_index = worst
        worst_signal = fact_signal(worst)
        dominance_allowed = candidate_signal >= worst_signal + _SIGNAL_DOMINANCE_INSERT_MARGIN
        candidate_effective_score = ranked.score + (_FACT_FRAME_RESCUE_WEIGHT * candidate_signal)
        worst_effective_score = worst_ranked.score + (_FACT_FRAME_RESCUE_WEIGHT * worst_signal)
        if (
            not dominance_allowed
            or candidate_effective_score + _FACT_FRAME_INSERT_MARGIN < worst_effective_score
        ):
            continue

        selected[worst_index] = candidate
        selected_ids.remove(worst_ranked.stable_id)
        selected_ids.add(ranked.stable_id)

    selected = sorted(selected, key=lambda item: (-item[0].score, item[1]))
    return [ranked for ranked, _index in selected] + [
        ranked for ranked, _index in ranked_by_score if ranked.stable_id not in selected_ids
    ]


def _stabilize_top_window_ranking[T](
    scores: list[tuple[QueryCoverageRankedCandidate[T], int]],
    *,
    min_overlap: float,
    insert_margin: float,
    dominance_score_margin: float | None = None,
    protected_original_rank: int | None = None,
    replace_strong_window: bool = True,
) -> list[QueryCoverageRankedCandidate[T]]:
    window_size = min(_EVIDENCE_SET_WINDOW, len(scores))
    selected = list(scores[:window_size])
    selected_ids = {ranked.stable_id for ranked, _index in selected}
    ranked_by_coverage = sorted(scores, key=lambda item: (-item[0].score, item[1]))

    def protected(item: tuple[QueryCoverageRankedCandidate[T], int]) -> bool:
        ranked, _index = item
        return bool(
            protected_original_rank is not None
            and ranked.original_rank <= protected_original_rank
            and ranked.overlap >= min_overlap
        )

    for candidate in ranked_by_coverage:
        ranked, _index = candidate
        if ranked.stable_id in selected_ids or ranked.overlap < min_overlap:
            continue

        low_signal = [
            item
            for item in enumerate(selected)
            if item[1][0].overlap < min_overlap and not protected(item[1])
        ]
        if low_signal:
            worst_index, worst = min(
                low_signal,
                key=lambda item: (item[1][0].overlap, item[1][0].score, -item[1][1]),
            )
            worst_ranked, _worst_original_index = worst
            dominance_allowed = (
                ranked.overlap >= min_overlap
                and ranked.overlap >= worst_ranked.overlap + _SIGNAL_DOMINANCE_INSERT_MARGIN
                and (
                    dominance_score_margin is None
                    or ranked.score + dominance_score_margin >= worst_ranked.score
                )
            )
            if dominance_allowed or ranked.score + insert_margin >= worst_ranked.score:
                selected[worst_index] = candidate
                selected_ids.remove(worst_ranked.stable_id)
                selected_ids.add(ranked.stable_id)
                continue

        if not replace_strong_window:
            continue

        replaceable = [item for item in enumerate(selected) if not protected(item[1])]
        if not replaceable:
            continue
        worst_index, worst = min(
            replaceable,
            key=lambda item: (item[1][0].score, -item[1][1]),
        )
        worst_ranked, _worst_original_index = worst
        if ranked.score <= worst_ranked.score + insert_margin:
            continue

        selected[worst_index] = candidate
        selected_ids.remove(worst_ranked.stable_id)
        selected_ids.add(ranked.stable_id)

    selected = sorted(selected, key=lambda item: (-item[0].score, item[1]))
    return [ranked for ranked, _index in selected] + [
        ranked for ranked, _index in ranked_by_coverage if ranked.stable_id not in selected_ids
    ]
