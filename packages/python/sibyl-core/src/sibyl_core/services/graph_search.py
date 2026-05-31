"""Search, list-filter, and task-progress helpers for native graph managers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from difflib import SequenceMatcher
from typing import Any

from sibyl_core.models.entities import Entity

type SurrealRecord = dict[str, object]

_TASK_PRIORITY_ORDER = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "someday": 4,
}
_SEARCH_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def row_score(row: SurrealRecord) -> float:
    score = row.get("score")
    if isinstance(score, int | float):
        return float(score)
    return 1.0


def merge_ranked_entity_results(
    ranked_lists: Sequence[tuple[Sequence[tuple[Entity, float]], float]],
    *,
    limit: int,
    rrf_k: float = 60.0,
) -> list[tuple[Entity, float]]:
    scores: dict[str, float] = {}
    entities: dict[str, Entity] = {}
    original_scores: dict[str, float] = {}

    for results, weight in ranked_lists:
        for rank, (entity, score) in enumerate(results, start=1):
            key = entity.id
            scores[key] = scores.get(key, 0.0) + (weight / (rrf_k + rank))
            original_scores[key] = max(original_scores.get(key, 0.0), score)
            entities.setdefault(key, entity)

    ordered = sorted(
        scores,
        key=lambda key: (scores[key], original_scores[key], entities[key].created_at, key),
        reverse=True,
    )
    return [(entities[key], scores[key]) for key in ordered[: max(int(limit), 1)]]


def entity_matches_list_filters(
    entity: Entity,
    *,
    project_id: str | None,
    epic_id: str | None,
    no_epic: bool,
    parent_task_id: str | None = None,
    status_values: Sequence[str],
    priority_values: Sequence[str],
    complexity_values: Sequence[str],
    feature: str | None,
    tag_values: Sequence[str],
    include_archived: bool,
) -> bool:
    if project_id and metadata_scalar(entity, "project_id") != project_id:
        return False
    entity_parent_task_id = metadata_scalar(entity, "parent_task_id")
    entity_epic_id = metadata_scalar(entity, "epic_id")
    entity_epic_alias = entity_parent_task_id or entity_epic_id
    if epic_id and entity_epic_alias != epic_id and entity_epic_id != epic_id:
        return False
    if no_epic and (entity_parent_task_id or entity_epic_id):
        return False
    if parent_task_id and metadata_scalar(entity, "parent_task_id") != parent_task_id:
        return False
    entity_status = metadata_scalar(entity, "status")
    if status_values and str(entity_status or "").lower() not in status_values:
        return False
    if not include_archived and str(entity_status or "").lower() == "archived":
        return False
    if (
        priority_values
        and str(metadata_scalar(entity, "priority") or "").lower() not in priority_values
    ):
        return False
    if (
        complexity_values
        and str(metadata_scalar(entity, "complexity") or "").lower() not in complexity_values
    ):
        return False
    if feature and str(metadata_scalar(entity, "feature") or "").lower() != feature.lower():
        return False
    if tag_values:
        entity_tags = metadata_str_values(entity, "tags")
        if not any(tag in entity_tags for tag in tag_values):
            return False
    return True


def metadata_scalar(entity: Entity, key: str) -> object | None:
    return dict(entity.metadata or {}).get(key)


def metadata_str_values(entity: Entity, key: str) -> list[str]:
    value = metadata_scalar(entity, key)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value.lower()]
        if isinstance(parsed, list):
            return [str(item).lower() for item in parsed if str(item)]
        return [value.lower()]
    if isinstance(value, Iterable) and not isinstance(value, bytes | dict):
        return [str(item).lower() for item in value if str(item)]
    return []


def new_task_progress() -> dict[str, int]:
    return {
        "total_tasks": 0,
        "completed_tasks": 0,
        "in_progress_tasks": 0,
        "blocked_tasks": 0,
        "in_review_tasks": 0,
    }


def count_task_progress(counters: dict[str, int], task: Entity) -> None:
    count_task_status(counters, metadata_scalar(task, "status"))


def count_task_status(
    counters: dict[str, int],
    status: object | None,
    *,
    count: int = 1,
) -> None:
    if count <= 0:
        return
    counters["total_tasks"] += count
    status_value = str(status or "").lower()
    if status_value == "done":
        counters["completed_tasks"] += count
    elif status_value == "doing":
        counters["in_progress_tasks"] += count
    elif status_value == "blocked":
        counters["blocked_tasks"] += count
    elif status_value == "review":
        counters["in_review_tasks"] += count


def finalize_task_progress(counters: dict[str, int]) -> dict[str, Any]:
    total = counters["total_tasks"]
    completed = counters["completed_tasks"]
    return {
        **counters,
        "completion_pct": round((completed / total * 100) if total > 0 else 0, 1),
    }


def lower_filter_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip().lower() for part in value.split(",") if part.strip()]


def task_priority_rank(task_info: dict[str, Any]) -> int:
    return _TASK_PRIORITY_ORDER.get(str(task_info.get("priority") or "").lower(), 99)


def lower_sequence_values(values: Sequence[str] | None) -> list[str]:
    return [str(value).strip().lower() for value in values or () if str(value).strip()]


def bounded_similarity_score(query: str, entity: Entity) -> float:
    query_text = normalize_search_text(query)
    entity_text = normalize_search_text(
        " ".join(
            part
            for part in (
                entity.name,
                entity.description,
                entity.content,
                str(entity.metadata.get("summary") or ""),
            )
            if part
        )
    )
    if not query_text or not entity_text:
        return 0.0
    if query_text in entity_text or entity_text in query_text:
        return 1.0

    query_tokens = set(_SEARCH_TOKEN_RE.findall(query_text))
    entity_tokens = set(_SEARCH_TOKEN_RE.findall(entity_text))
    if not query_tokens or not entity_tokens:
        return 0.0

    overlap = query_tokens & entity_tokens
    jaccard = len(overlap) / len(query_tokens | entity_tokens)
    coverage = len(overlap) / len(query_tokens)
    sequence = SequenceMatcher(None, query_text[:1000], entity_text[:1000]).ratio()
    return min(max(jaccard, coverage * 0.85, sequence * 0.9), 1.0)


def normalize_search_text(value: str) -> str:
    return " ".join(_SEARCH_TOKEN_RE.findall(value.lower()))


__all__ = [
    "bounded_similarity_score",
    "count_task_progress",
    "count_task_status",
    "entity_matches_list_filters",
    "finalize_task_progress",
    "lower_filter_values",
    "lower_sequence_values",
    "merge_ranked_entity_results",
    "metadata_scalar",
    "metadata_str_values",
    "new_task_progress",
    "normalize_search_text",
    "row_score",
    "task_priority_rank",
]
