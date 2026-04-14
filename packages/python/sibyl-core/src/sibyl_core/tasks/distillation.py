from __future__ import annotations

import re
from collections.abc import Sequence

from sibyl_core.models.entities import EntityType, Episode, Procedure, ProcedureStep
from sibyl_core.models.tasks import Task

_MARKDOWN_PREFIX_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)]|step\s+\d+[:.)-]?)\s*", re.IGNORECASE)
_STEP_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+")
_SENTENCE_PREFIX_RE = re.compile(
    r"^\s*(?:first|then|next|after that|finally|lastly)[,:-]?\s*",
    re.IGNORECASE,
)
_METADATA_LINE_RE = re.compile(r"^\s*(?:##|###|\*\*.+?\*\*|status:|feature:|technologies:)", re.IGNORECASE)


def build_learning_episode(task: Task) -> Episode:
    """Convert a completed task into a learning episode."""
    content_parts = [
        f"## Task: {task.title}",
        "",
        f"**Status**: {task.status}",
        f"**Feature**: {task.feature or 'N/A'}",
        f"**Technologies**: {', '.join(task.technologies)}",
    ]

    if task.actual_hours:
        content_parts.append(f"**Time Spent**: {task.actual_hours} hours")

    if task.estimated_hours and task.actual_hours:
        accuracy = (task.estimated_hours / task.actual_hours) * 100
        content_parts.append(f"**Estimation Accuracy**: {accuracy:.1f}%")

    content_parts.extend(
        [
            "",
            "### What Was Done",
            "",
            task.description,
            "",
            "### Learnings",
            "",
            task.learnings or "",
        ]
    )

    if task.blockers_encountered:
        content_parts.extend(
            [
                "",
                "### Blockers Encountered",
                "",
            ]
        )
        content_parts.extend(f"- {blocker}" for blocker in task.blockers_encountered)

    if task.commit_shas:
        content_parts.extend(
            [
                "",
                "### Related Commits",
                "",
            ]
        )
        content_parts.extend(f"- `{sha}`" for sha in task.commit_shas)

    return Episode(
        id=f"episode_{task.id}",
        entity_type=EntityType.EPISODE,
        name=f"Task Completed: {task.title}",
        description=task.description,
        content="\n".join(content_parts),
        episode_type="task_completion",
        metadata={
            "task_id": task.id,
            "project_id": task.project_id,
            "feature": task.feature,
            "technologies": task.technologies,
            "complexity": task.complexity.value if task.complexity else None,
            "estimated_hours": task.estimated_hours,
            "actual_hours": task.actual_hours,
            "estimation_accuracy": (
                task.estimated_hours / task.actual_hours
                if task.estimated_hours and task.actual_hours
                else None
            ),
        },
        valid_from=task.completed_at,
    )


def build_learning_procedure(task: Task, note_contents: Sequence[str] = ()) -> Procedure | None:
    """Distill a reusable procedure from task learnings and notes."""
    steps, mode = _distill_steps(task, note_contents)
    if not steps:
        return None

    summary = task.learnings.strip() or task.description.strip() or task.title
    content_lines = [
        f"## Procedure: {task.title}",
        "",
        "### Steps",
        "",
    ]
    content_lines.extend(f"{step.order}. {step.description}" for step in steps)
    content_lines.extend(
        [
            "",
            "### Distilled From",
            "",
            f"- Task: {task.title}",
            f"- Learnings: {summary}",
        ]
    )

    estimated_minutes = int(task.actual_hours * 60) if task.actual_hours else None

    return Procedure(
        id=f"procedure_{task.id}",
        entity_type=EntityType.PROCEDURE,
        name=f"Procedure: {task.title}",
        description=summary[:240],
        content="\n".join(content_lines),
        category=task.domain or task.feature or "workflow",
        required_tools=list(task.technologies or []),
        estimated_minutes=estimated_minutes,
        automation_level="manual",
        steps=steps,
        metadata={
            "task_id": task.id,
            "project_id": task.project_id,
            "feature": task.feature,
            "technologies": task.technologies,
            "complexity": task.complexity.value if task.complexity else None,
            "distillation_mode": mode,
            "notes_used": len([note for note in note_contents if note.strip()]),
        },
    )


def _distill_steps(task: Task, note_contents: Sequence[str]) -> tuple[list[ProcedureStep], str]:
    explicit = _extract_explicit_steps([task.learnings, *note_contents])
    if explicit:
        return explicit, "explicit_steps"

    fallback_inputs = [task.description, task.learnings, *note_contents]
    fallback = _extract_fallback_steps(fallback_inputs)
    if fallback:
        return fallback, "fallback_sentences"

    return [], "none"


def _extract_explicit_steps(texts: Sequence[str]) -> list[ProcedureStep]:
    candidates: list[str] = []
    for text in texts:
        if not text:
            continue
        for raw_line in text.splitlines():
            normalized = raw_line.strip()
            if not normalized:
                continue
            if _METADATA_LINE_RE.match(normalized):
                continue
            if normalized.lower().startswith("learned"):
                continue
            if match := _MARKDOWN_PREFIX_RE.match(normalized):
                step_text = normalized[match.end() :].strip()
                if step_text:
                    candidates.append(step_text)

    if not candidates:
        return []

    return _to_steps(candidates)


def _extract_fallback_steps(texts: Sequence[str]) -> list[ProcedureStep]:
    candidates: list[str] = []
    for text in texts:
        if not text:
            continue
        normalized = " ".join(part.strip() for part in text.splitlines() if part.strip())
        if not normalized:
            continue
        for fragment in _STEP_SPLIT_RE.split(normalized):
            cleaned = _SENTENCE_PREFIX_RE.sub("", fragment.strip())
            cleaned = cleaned.strip(" -")
            if len(cleaned) < 12:
                continue
            candidates.append(cleaned)

    if len(candidates) < 2:
        return []

    return _to_steps(candidates[:5])


def _to_steps(candidates: Sequence[str]) -> list[ProcedureStep]:
    steps: list[ProcedureStep] = []
    seen: set[str] = set()
    for index, candidate in enumerate(candidates, start=1):
        normalized = candidate.strip().rstrip(".")
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        title = normalized.split(":")[0].strip()
        title = title[:72] if len(title) > 72 else title
        steps.append(
            ProcedureStep(
                order=index,
                title=title,
                description=normalized,
            )
        )
    return steps
