"""Deterministic feedback queries for iterative evidence retrieval."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace

from sibyl_core.query_anchors import extract_explicit_query_anchors
from sibyl_core.retrieval.query_ranking import extract_keywords

MAX_FEEDBACK_DOCUMENTS = 8
MAX_REFINEMENT_QUERIES = 3
MAX_REFINEMENT_TERMS = 4
MAX_REFINEMENT_QUERY_CHARS = 500
_FEEDBACK_STOPWORDS = {
    "assistant",
    "conversation",
    "evidence",
    "goal",
    "memory",
    "question",
    "session",
    "transcript",
    "user",
}
_STRUCTURAL_COUNTER_PATTERN = re.compile(
    r"^[A-Za-z][\w ]* \d+(?: part \d+/\d+)?$",
    re.I,
)
_ACCESSIBILITY_NODE_PATTERN = re.compile(
    r"^(?:\[[^\]]+\]\s+)?[A-Za-z][\w-]*\s+"
    r"(?P<quote>['\"])(?P<name>.*?)(?P=quote)(?:,.*)?$"
)
_UNNAMED_ACCESSIBILITY_NODE_PATTERN = re.compile(
    r"^(?:(?:\[[^\]]+\]\s+)?[A-Za-z][\w-]*,.*|"
    r"\[[^\]]+\]\s+[A-Za-z][\w-]*)$"
)
_MIME_TYPE_PATTERN = re.compile(r"^[\w.+-]+/[\w.+-]+(?:\s*;.*)?$", re.I)
_OPAQUE_IDENTIFIER_PATTERN = re.compile(r"(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_-]{8,}$")
_URL_PATTERN = re.compile(r"https?://\S+", re.I)
_RESPONSE_COMMAND = (
    r"(?:answer|format|give|list|mark|provide|put|return|respond|say|separate|wrap|write)"
)
_ANSWER_OBJECT = (
    r"(?:(?:my|our|your)\s+(?:final\s+)?(?:answers?|outputs?|responses?)|"
    r"(?:the\s+)?final\s+(?:answers?|outputs?|responses?))"
)
_UNAMBIGUOUS_OUTPUT_FORMAT = r"(?:\\boxed|\bin\s+(?:this\s+order|the\s+following\s+order)\b)"
_ANSWER_RELATIVE_FORMAT = (
    r"(?:(?:comma[- ]separated|semicolon[- ]separated|separat\w+\s+by|"
    r"\bin\s+(?:english|a\s+list)\b|"
    r"\b(?:formatted|marked|wrapped)\s+(?:as|in|with)\b)"
    r"|\b(?:no explanation|one word|short phrases?)\b)"
)
_RESPONSE_INSTRUCTION_PATTERN = re.compile(
    rf"^(?:please\s+)?{_RESPONSE_COMMAND}\b.*{_UNAMBIGUOUS_OUTPUT_FORMAT}"
    rf"|^(?:please\s+)?(?:answer|respond)\b.*{_ANSWER_RELATIVE_FORMAT}"
    rf"|^(?:please\s+)?{_RESPONSE_COMMAND}\b.*\b{_ANSWER_OBJECT}\b.*{_ANSWER_RELATIVE_FORMAT}"
    rf"|^(?:please\s+)?(?:put|wrap)\b.*\b{_ANSWER_OBJECT}\b"
    rf"|^(?:please\s+)?say\s+the\s+answer\b.*{_ANSWER_RELATIVE_FORMAT}"
    rf"|^(?:your|the) final answers?\b.*(?:{_UNAMBIGUOUS_OUTPUT_FORMAT}|{_ANSWER_RELATIVE_FORMAT})",
    re.I,
)
_QUERY_CLAUSE_SPLIT_PATTERN = re.compile(
    rf"(?<=[.!?])\s+|(?<=[;,:])\s+(?=(?:please\s+)?{_RESPONSE_COMMAND}\b)",
    re.I,
)
_CONJUNCTION_COMMAND_PATTERN = re.compile(
    rf"\s+and\s+(?=(?:please\s+)?{_RESPONSE_COMMAND}\b)",
    re.I,
)
_ENUMERATED_TARGETS_PATTERN = re.compile(
    r"\bfor\s+(?:the\s+)?"
    r"(?P<targets>(?:(?![.!?]).){1,240}?\b(?:lists?|pages?|panes?))"
    r"(?=[.!?])",
    re.I,
)
_INTERROGATIVE_CLAUSE_PATTERN = re.compile(
    r"^(?:how|what|when|where|which|who|why)\b",
    re.I,
)
_COORDINATED_TERMS_PATTERN = re.compile(
    r"\b(?P<left>[A-Za-z][\w-]{2,})(?:\s+(?:and|or)\s+|\s*/\s*)"
    r"(?P<right>[A-Za-z][\w-]{2,})\b",
    re.I,
)
_TARGET_SUFFIX_PATTERN = re.compile(r"\b(?:lists?|pages?|panes?)$", re.I)
_OPERATIONAL_PROJECTION_KINDS = {
    "manifest",
    "procedure",
    "raw_observation",
    "reported_failure",
    "transition",
}
_OPERATIONAL_ENVELOPE_LABELS = {
    "action",
    "after uri",
    "before uri",
    "domain",
    "environment",
    "evidence content type",
    "final action",
    "outcome",
    "reported outcome",
    "screenshot",
    "start url",
    "state",
    "trajectory",
    "uri",
    "url",
}


@dataclass(frozen=True, slots=True)
class RetrievalFeedbackDocument:
    """One retrieved item used to derive the next search frontier."""

    id: str
    text: str
    source_id: str | None = None
    raw_observation_projection: bool = False
    evidence_content_type: str | None = None
    projection_kind: str | None = None


@dataclass(frozen=True, slots=True)
class DeterministicRefinementQuery:
    """A supplemental query derived from the question and retrieved evidence."""

    query: str
    facet: str
    source_result_ids: tuple[str, ...] = ()
    added_terms: tuple[str, ...] = ()


def plan_deterministic_refinement_queries(
    question: str,
    documents: Sequence[RetrievalFeedbackDocument],
    *,
    max_queries: int,
    seen_queries: Sequence[str] = (),
) -> list[DeterministicRefinementQuery]:
    """Build focused and feedback-expanded searches without a generative model."""
    question = normalize_retrieval_question(question)
    if not question:
        raise ValueError("question must not be empty")
    if max_queries < 1:
        return []

    question_terms = extract_keywords(question)
    question_term_set = set(question_terms)
    seen = {query.casefold() for query in seen_queries}
    seen.add(question.casefold())
    planned: list[DeterministicRefinementQuery] = []

    explicit_anchors = extract_explicit_query_anchors(question)
    for candidate in _structural_refinement_queries(
        question,
        explicit_anchors=explicit_anchors,
    ):
        _append_query(planned, seen, candidate, max_queries=max_queries)
    if len(planned) >= max_queries:
        return planned

    explicit_anchor_terms = tuple(
        dict.fromkeys(term for anchor in explicit_anchors for term in anchor)
    )
    if explicit_anchor_terms:
        _append_query(
            planned,
            seen,
            DeterministicRefinementQuery(
                query=_bounded_query(
                    " ".join(f'"{" ".join(anchor)}"' for anchor in explicit_anchors)
                ),
                facet="anchor",
                added_terms=explicit_anchor_terms,
            ),
            max_queries=max_queries,
        )

    if len(planned) >= max_queries:
        return planned

    focused_query = _bounded_query(" ".join(question_terms))
    if len(question_terms) >= 2:
        _append_query(
            planned,
            seen,
            DeterministicRefinementQuery(query=focused_query, facet="focus"),
            max_queries=max_queries,
        )

    if len(planned) >= max_queries:
        return planned

    ranked_terms, document_terms = _rank_feedback_terms(
        documents,
        excluded_terms=question_term_set,
    )
    if not ranked_terms:
        return planned

    document_count = len(document_terms)
    high_frequency_cutoff = (
        ((3 * document_count) + 3) // 4 if document_count >= 3 else document_count + 1
    )
    consensus_terms = [
        term
        for term in ranked_terms
        if 1 < _document_frequency(term, document_terms) < high_frequency_cutoff
    ]
    term_groups = [("corroboration", consensus_terms)]
    if document_count == 1:
        term_groups.append(
            ("feedback", [term for term in ranked_terms if term not in consensus_terms])
        )
    for facet, candidates in term_groups:
        if not candidates or len(planned) >= max_queries:
            continue
        terms = tuple(candidates[:MAX_REFINEMENT_TERMS])
        source_result_ids = _source_ids_for_terms(terms, documents, document_terms)
        _append_query(
            planned,
            seen,
            DeterministicRefinementQuery(
                query=_expanded_query(focused_query or question, terms),
                facet=facet,
                source_result_ids=source_result_ids,
                added_terms=terms,
            ),
            max_queries=max_queries,
        )

    return planned


def _structural_refinement_queries(
    question: str,
    *,
    explicit_anchors: Sequence[Sequence[str]],
) -> list[DeterministicRefinementQuery]:
    targets = _enumerated_targets(question)
    terminal_clause = _terminal_interrogative_clause(question)
    candidates: list[DeterministicRefinementQuery] = []
    if targets:
        target_terms = set(extract_keywords(" ".join(targets)))
        intent_terms = [
            term
            for term in extract_keywords(terminal_clause or question)
            if term not in target_terms
        ]
        anchor_query = " ".join(f'"{" ".join(anchor)}"' for anchor in explicit_anchors)
        for target in targets:
            candidates.append(
                DeterministicRefinementQuery(
                    query=_bounded_query(
                        " ".join(
                            part
                            for part in (
                                anchor_query,
                                f'"{target}"',
                                " ".join(intent_terms),
                            )
                            if part
                        )
                    ),
                    facet="target",
                    added_terms=tuple(extract_keywords(target)),
                )
            )
        return candidates

    if terminal_clause and explicit_anchors:
        terminal_terms = _structural_keywords(terminal_clause)
        normalized_terminal = terminal_clause.casefold()
        omitted_anchor = any(
            " ".join(anchor).casefold() not in normalized_terminal for anchor in explicit_anchors
        )
        if omitted_anchor and len(terminal_terms) >= 4:
            candidates.append(
                DeterministicRefinementQuery(
                    query=_bounded_query(" ".join(terminal_terms)),
                    facet="focus_clause",
                )
            )
    return candidates


def _structural_keywords(text: str) -> list[str]:
    coordinated_terms = {
        term.casefold()
        for match in _COORDINATED_TERMS_PATTERN.finditer(text)
        for term in (match.group("left"), match.group("right"))
    }
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z][\w-]*", text):
        extracted = extract_keywords(token)
        if extracted:
            term = extracted[0]
        elif token.casefold() in coordinated_terms:
            term = token.casefold()
        else:
            continue
        if term not in terms:
            terms.append(term)
    return terms


def _enumerated_targets(question: str) -> tuple[str, ...]:
    match = _ENUMERATED_TARGETS_PATTERN.search(question)
    if match is None:
        return ()
    raw_targets = re.sub(
        r"\s*,\s*(?:and|or)\s+",
        ",",
        match.group("targets"),
        flags=re.I,
    )
    targets = tuple(
        " ".join(target.strip(" ,;:").split())
        for target in re.split(r"\s*,\s*|\s+(?:and|or)\s+", raw_targets, flags=re.I)
        if target.strip(" ,;:")
    )
    if len(targets) < 2 or any(
        len(target.split()) > 8 or _TARGET_SUFFIX_PATTERN.search(target) is None
        for target in targets
    ):
        return ()
    return targets


def _terminal_interrogative_clause(question: str) -> str:
    clauses = _split_query_clauses(question)
    if len(clauses) < 2:
        return ""
    for clause in reversed(clauses):
        normalized = clause.strip(" ,;:")
        if _INTERROGATIVE_CLAUSE_PATTERN.match(normalized):
            return normalized
    return ""


def _rank_feedback_terms(
    documents: Sequence[RetrievalFeedbackDocument],
    *,
    excluded_terms: set[str],
) -> tuple[list[str], dict[str, set[str]]]:
    scores: dict[str, float] = defaultdict(float)
    document_terms: dict[str, set[str]] = {}
    first_seen: dict[str, tuple[int, int]] = {}
    source_ids: set[str] = set()
    source_documents: list[RetrievalFeedbackDocument] = []
    for document in documents:
        source_id = document.source_id or document.id
        if source_id in source_ids:
            continue
        source_ids.add(source_id)
        source_documents.append(document)
        if len(source_documents) >= MAX_FEEDBACK_DOCUMENTS:
            break

    for rank, document in enumerate(source_documents, start=1):
        ordered_terms = list(
            dict.fromkeys(
                term
                for term in extract_keywords(_feedback_text(document))
                if term not in excluded_terms
                and term not in _FEEDBACK_STOPWORDS
                and _is_feedback_term(term)
            )
        )
        terms = set(ordered_terms)
        document_terms[document.id] = terms
        rank_weight = 1.0 / rank
        for position, term in enumerate(ordered_terms):
            scores[term] += rank_weight
            first_seen.setdefault(term, (rank, position))

    ranked = sorted(
        scores,
        key=lambda term: (
            -scores[term],
            -_document_frequency(term, document_terms),
            *first_seen[term],
            term,
        ),
    )
    return ranked, document_terms


def _feedback_text(document: RetrievalFeedbackDocument) -> str:
    raw_lines = _feedback_lines(document)
    lines: list[str] = []
    accessibility_profile = bool(
        document.evidence_content_type
        and "profile=accessibility-tree" in document.evidence_content_type.casefold()
    )
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line:
            continue
        label, separator, value = line.partition(":")
        if (
            separator
            and (
                document.raw_observation_projection
                or document.projection_kind in _OPERATIONAL_PROJECTION_KINDS
            )
            and label.strip().casefold() in _OPERATIONAL_ENVELOPE_LABELS
        ):
            continue
        if accessibility_profile:
            node_match = _ACCESSIBILITY_NODE_PATTERN.fullmatch(line)
            if node_match is not None:
                line = node_match.group("name")
            elif (
                line.endswith(":")
                or _STRUCTURAL_COUNTER_PATTERN.fullmatch(line)
                or _UNNAMED_ACCESSIBILITY_NODE_PATTERN.fullmatch(line)
            ):
                continue
            else:
                if separator:
                    line = value.strip()
                    if not _useful_header_value(line):
                        continue
        line = _URL_PATTERN.sub(" ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _feedback_lines(document: RetrievalFeedbackDocument) -> list[str]:
    if not document.raw_observation_projection:
        lines = document.text.splitlines()
        if document.projection_kind == "manifest":
            return []
        if document.projection_kind == "procedure":
            return [line for line in lines if line.lstrip().casefold().startswith("goal:")]
        return lines

    _header, separator, evidence = document.text.partition("\nEvidence:\n")
    if not separator:
        return []
    return evidence.splitlines()


def normalize_retrieval_question(question: str) -> str:
    normalized = " ".join(question.split())
    clauses = _split_query_clauses(normalized)
    retained = [
        clause.strip(" ,;:")
        for clause in clauses
        if clause.strip(" ,;:") and not _RESPONSE_INSTRUCTION_PATTERN.search(clause)
    ]
    stripped = " ".join(retained)
    return " ".join(stripped.split()) or normalized


def _split_query_clauses(question: str) -> list[str]:
    clauses: list[str] = []
    for clause in _QUERY_CLAUSE_SPLIT_PATTERN.split(question):
        clauses.extend(_split_conjunction_clauses(clause))
    return clauses


def _split_conjunction_clauses(clause: str) -> list[str]:
    conjunctions = list(_CONJUNCTION_COMMAND_PATTERN.finditer(clause))
    for conjunction in conjunctions:
        prefix = clause[: conjunction.start()]
        if _RESPONSE_INSTRUCTION_PATTERN.search(prefix):
            return [
                *_split_conjunction_clauses(prefix),
                *_split_conjunction_clauses(clause[conjunction.end() :]),
            ]
    for conjunction in reversed(conjunctions):
        suffix = clause[conjunction.end() :]
        if _RESPONSE_INSTRUCTION_PATTERN.search(suffix):
            return [
                *_split_conjunction_clauses(clause[: conjunction.start()]),
                *_split_conjunction_clauses(suffix),
            ]
    return [clause]


def _is_feedback_term(term: str) -> bool:
    if _OPAQUE_IDENTIFIER_PATTERN.fullmatch(term):
        return False
    alpha_count = sum(character.isalpha() for character in term)
    digit_count = sum(character.isdigit() for character in term)
    return alpha_count >= 3 and (digit_count == 0 or alpha_count >= digit_count)


def _useful_header_value(value: str) -> bool:
    normalized = _URL_PATTERN.sub(" ", value).strip()
    return bool(
        normalized
        and not normalized.isdigit()
        and not re.fullmatch(r"\d+/\d+", normalized)
        and not _MIME_TYPE_PATTERN.fullmatch(normalized)
        and not _OPAQUE_IDENTIFIER_PATTERN.fullmatch(normalized)
    )


def _document_frequency(term: str, document_terms: dict[str, set[str]]) -> int:
    return sum(term in terms for terms in document_terms.values())


def _source_ids_for_terms(
    terms: Sequence[str],
    documents: Sequence[RetrievalFeedbackDocument],
    document_terms: dict[str, set[str]],
) -> tuple[str, ...]:
    selected = set(terms)
    return tuple(
        document.id for document in documents if selected & document_terms.get(document.id, set())
    )


def _expanded_query(question: str, terms: Sequence[str]) -> str:
    suffix = " ".join(terms)
    max_question_chars = max(0, MAX_REFINEMENT_QUERY_CHARS - len(suffix) - 1)
    question_prefix = _bounded_text(question, max_chars=max_question_chars)
    return _bounded_query(f"{question_prefix} {suffix}".strip())


def _bounded_query(query: str) -> str:
    return _bounded_text(query, max_chars=MAX_REFINEMENT_QUERY_CHARS)


def _bounded_text(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    prefix = text[: max_chars + 1]
    bounded, separator, _remainder = prefix.rpartition(" ")
    return bounded if separator else text[:max_chars]


def _append_query(
    planned: list[DeterministicRefinementQuery],
    seen: set[str],
    candidate: DeterministicRefinementQuery,
    *,
    max_queries: int,
) -> None:
    normalized = " ".join(candidate.query.split())
    key = normalized.casefold()
    if len(normalized) < 3 or key in seen or len(planned) >= max_queries:
        return
    seen.add(key)
    planned.append(
        candidate if normalized == candidate.query else replace(candidate, query=normalized)
    )


__all__ = [
    "MAX_REFINEMENT_QUERIES",
    "DeterministicRefinementQuery",
    "RetrievalFeedbackDocument",
    "normalize_retrieval_question",
    "plan_deterministic_refinement_queries",
]
