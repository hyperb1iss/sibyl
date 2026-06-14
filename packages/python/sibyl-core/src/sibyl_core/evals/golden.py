"""Golden dataset contracts for retrieval and context-pack evals."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sibyl_core.evals.metrics import EvalQuery

GOLDEN_EVAL_SCHEMA_VERSION = "sibyl-golden-eval-dataset/v1"


@dataclass(frozen=True, slots=True)
class GoldenTextLabel:
    kind: str
    value: str
    grade: int = 1


@dataclass(frozen=True, slots=True)
class GoldenDocument:
    id: str
    title: str
    content: str
    expected_scope: str
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GoldenRetrievalQuery:
    id: str
    query: str
    expected_ids: tuple[str, ...]
    relevance_grades: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_eval_query(self) -> EvalQuery:
        return EvalQuery(
            query=self.query,
            expected_ids=list(self.expected_ids),
            relevance_grades=dict(self.relevance_grades),
            metadata={"id": self.id, **self.metadata},
        )


@dataclass(frozen=True, slots=True)
class GoldenContextCase:
    id: str
    case: str
    positive_document_ids: tuple[str, ...]
    negative_document_ids: tuple[str, ...]
    labels: tuple[GoldenTextLabel, ...] = ()
    negative_labels: tuple[GoldenTextLabel, ...] = ()


@dataclass(frozen=True, slots=True)
class GoldenEvalDataset:
    schema_version: str
    dataset_id: str
    version: str
    description: str
    corpus: dict[str, Any]
    documents: tuple[GoldenDocument, ...]
    retrieval_queries: tuple[GoldenRetrievalQuery, ...]
    context_pack_cases: tuple[GoldenContextCase, ...]

    @property
    def document_ids(self) -> set[str]:
        return {document.id for document in self.documents}

    @property
    def context_case_names(self) -> set[str]:
        return {case.case for case in self.context_pack_cases}

    def to_eval_queries(self) -> list[EvalQuery]:
        return [query.to_eval_query() for query in self.retrieval_queries]


def load_golden_eval_dataset(path: Path) -> GoldenEvalDataset:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = "golden eval dataset must be a JSON object"
        raise ValueError(msg)

    dataset = GoldenEvalDataset(
        schema_version=_required_string(data, "schema_version"),
        dataset_id=_required_string(data, "dataset_id"),
        version=_required_string(data, "version"),
        description=_required_string(data, "description"),
        corpus=_mapping(data.get("corpus"), "corpus"),
        documents=tuple(_document_from_dict(item) for item in _list(data, "documents")),
        retrieval_queries=tuple(
            _retrieval_query_from_dict(item) for item in _list(data, "retrieval_queries")
        ),
        context_pack_cases=tuple(
            _context_case_from_dict(item) for item in _list(data, "context_pack_cases")
        ),
    )
    _validate_dataset(dataset)
    return dataset


def _validate_dataset(dataset: GoldenEvalDataset) -> None:
    if dataset.schema_version != GOLDEN_EVAL_SCHEMA_VERSION:
        msg = (
            f"unsupported golden eval schema {dataset.schema_version!r}; "
            f"expected {GOLDEN_EVAL_SCHEMA_VERSION!r}"
        )
        raise ValueError(msg)

    document_ids = _unique("document id", (document.id for document in dataset.documents))
    _unique("retrieval query id", (query.id for query in dataset.retrieval_queries))
    _unique("context-pack case id", (case.id for case in dataset.context_pack_cases))
    _unique("context-pack case name", (case.case for case in dataset.context_pack_cases))

    for query in dataset.retrieval_queries:
        if not query.expected_ids:
            msg = f"retrieval query {query.id!r} must name expected_ids"
            raise ValueError(msg)
        _require_known_ids(
            f"retrieval query {query.id!r} expected_ids",
            query.expected_ids,
            document_ids,
        )
        _require_known_ids(
            f"retrieval query {query.id!r} relevance_grades",
            query.relevance_grades,
            document_ids,
        )
        missing_grades = sorted(set(query.expected_ids) - set(query.relevance_grades))
        if missing_grades:
            msg = (
                f"retrieval query {query.id!r} lacks relevance grades for "
                f"{', '.join(missing_grades)}"
            )
            raise ValueError(msg)
        bad_grades = {
            item_id: grade for item_id, grade in query.relevance_grades.items() if grade <= 0
        }
        if bad_grades:
            msg = f"retrieval query {query.id!r} has non-positive grades: {bad_grades!r}"
            raise ValueError(msg)

    for case in dataset.context_pack_cases:
        _require_known_ids(
            f"context-pack case {case.case!r} positive_document_ids",
            case.positive_document_ids,
            document_ids,
        )
        _require_known_ids(
            f"context-pack case {case.case!r} negative_document_ids",
            case.negative_document_ids,
            document_ids,
        )
        if not case.positive_document_ids and not case.negative_document_ids and not case.labels:
            msg = f"context-pack case {case.case!r} must carry at least one label"
            raise ValueError(msg)


def _document_from_dict(value: Any) -> GoldenDocument:
    data = _mapping(value, "document")
    return GoldenDocument(
        id=_required_string(data, "id"),
        title=_required_string(data, "title"),
        content=_required_string(data, "content"),
        expected_scope=_required_string(data, "expected_scope"),
        tags=_string_tuple(data.get("tags"), "document.tags"),
        metadata=_mapping(data.get("metadata", {}), "document.metadata"),
    )


def _retrieval_query_from_dict(value: Any) -> GoldenRetrievalQuery:
    data = _mapping(value, "retrieval query")
    return GoldenRetrievalQuery(
        id=_required_string(data, "id"),
        query=_required_string(data, "query"),
        expected_ids=_string_tuple(data.get("expected_ids"), "retrieval_query.expected_ids"),
        relevance_grades=_grade_mapping(data.get("relevance_grades")),
        metadata=_mapping(data.get("metadata", {}), "retrieval_query.metadata"),
    )


def _context_case_from_dict(value: Any) -> GoldenContextCase:
    data = _mapping(value, "context-pack case")
    return GoldenContextCase(
        id=_required_string(data, "id"),
        case=_required_string(data, "case"),
        positive_document_ids=_string_tuple(
            data.get("positive_document_ids"),
            "context_pack_case.positive_document_ids",
        ),
        negative_document_ids=_string_tuple(
            data.get("negative_document_ids"),
            "context_pack_case.negative_document_ids",
        ),
        labels=_labels(data.get("labels")),
        negative_labels=_labels(data.get("negative_labels")),
    )


def _labels(value: Any) -> tuple[GoldenTextLabel, ...]:
    if value is None:
        return ()
    labels: list[GoldenTextLabel] = []
    for item in _list_value(value, "labels"):
        data = _mapping(item, "label")
        labels.append(
            GoldenTextLabel(
                kind=_required_string(data, "kind"),
                value=_required_string(data, "value"),
                grade=int(data.get("grade", 1)),
            )
        )
    return tuple(labels)


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        msg = f"golden eval dataset field {key!r} must be a non-empty string"
        raise ValueError(msg)
    return value


def _list(data: dict[str, Any], key: str) -> list[Any]:
    return _list_value(data.get(key), key)


def _list_value(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        msg = f"golden eval dataset field {label!r} must be a list"
        raise ValueError(msg)
    return value


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        msg = f"golden eval dataset field {label!r} must be an object"
        raise ValueError(msg)
    return {str(key): item for key, item in value.items()}


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    return tuple(str(item) for item in _list_value(value, label))


def _grade_mapping(value: Any) -> dict[str, int]:
    data = _mapping(value, "relevance_grades")
    return {item_id: int(grade) for item_id, grade in data.items()}


def _unique(label: str, values: Any) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for raw_value in values:
        value = str(raw_value)
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        msg = f"duplicate {label}s: {', '.join(sorted(duplicates))}"
        raise ValueError(msg)
    return seen


def _require_known_ids(label: str, values: Any, document_ids: set[str]) -> None:
    missing = sorted(str(value) for value in values if str(value) not in document_ids)
    if missing:
        msg = f"{label} reference unknown documents: {', '.join(missing)}"
        raise ValueError(msg)


__all__ = [
    "GOLDEN_EVAL_SCHEMA_VERSION",
    "GoldenContextCase",
    "GoldenDocument",
    "GoldenEvalDataset",
    "GoldenRetrievalQuery",
    "GoldenTextLabel",
    "load_golden_eval_dataset",
]
