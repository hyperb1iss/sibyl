"""LLM distillation of operational experience into reusable typed notes."""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints, model_validator
from pydantic_ai import Agent

from sibyl_core.ai.llm import Extractor, LLMSurface
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.models.experience import OperationalExperience, OperationalObservation

OPERATIONAL_NOTE_DISTILLATION_SCHEMA_VERSION = "sibyl-operational-note-distillation-v1"
OPERATIONAL_NOTE_CATEGORY = "operational_distillation"
MAX_OPERATIONAL_DIGEST_CHARS = 40_000
MAX_OPERATIONAL_NOTE_CHARS = 1_600
MAX_FACT_ITEMS = 10
MAX_GOTCHA_ITEMS = 5
MAX_CONTENT_LINES_PER_OBSERVATION = 8
MAX_CONTENT_LINES_TOTAL = 160
MAX_CONTENT_LINE_CHARS = 140
MIN_CONTENT_NAME_CHARS = 4

_CONTENT_NODE_RE = re.compile(r"(?:\[\w+\]\s+)?([A-Za-z]+)\s+'([^']{4,200})'")
_CONTENT_ROLES = {
    "heading": 3,
    "cell": 2,
    "gridcell": 2,
    "columnheader": 2,
    "rowheader": 2,
    "StaticText": 1,
    "link": 1,
    "option": 1,
    "listitem": 1,
    "article": 1,
}
_CONTENT_NOISE_RE = re.compile(
    r"skip to|accessibility preference|announcements displayed|global skip|"
    r"^navigation$|^primary$|unpinned|^menu$|^toolbar$|jump to",
    re.IGNORECASE,
)

OPERATIONAL_NOTE_DISTILLATION_SYSTEM_PROMPT = (
    "You distill agent trajectories into reusable operational memory notes for a future "
    "assistant working in the same environment. Write only what the trajectory itself "
    "evidences; never invent UI elements, labels, or outcomes. Prefer concrete names exactly "
    "as they appear, including form names, field labels, menu paths, list columns, and values."
)

_PROMPT_TEMPLATE = """Distill the trajectory below into exactly these fields:
- workflow: an imperative, step-by-step recipe for the performed task, naming exact UI labels;
  use an empty string when there is no coherent workflow
- facts: concrete, standalone environment facts with exact labels, names, values, and locations
- gotchas: pitfalls, errors, retries, or surprising behavior, each with its triggering condition

Limits: workflow <= 900 characters; at most {max_facts} facts; at most {max_gotchas} gotchas;
each fact or gotcha <= 300 characters.

Trajectory digest:
{digest}
"""

OperationalNoteItem = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]


class DistilledOperationalNotes(BaseModel):
    """Structured note payload produced from one operational experience."""

    workflow: str = Field(default="", max_length=900)
    facts: list[OperationalNoteItem] = Field(default_factory=list, max_length=MAX_FACT_ITEMS)
    gotchas: list[OperationalNoteItem] = Field(default_factory=list, max_length=MAX_GOTCHA_ITEMS)

    @model_validator(mode="after")
    def require_note_content(self) -> DistilledOperationalNotes:
        self.workflow = self.workflow.strip()
        if not self.workflow and not self.facts and not self.gotchas:
            raise ValueError("distillation output contained no notes")
        return self


def build_operational_experience_digest(
    experience: OperationalExperience,
    *,
    max_chars: int = MAX_OPERATIONAL_DIGEST_CHARS,
) -> str:
    """Render bounded source evidence for the distillation model."""
    lines = [
        f"Goal: {_clean(experience.goal)}",
        f"Outcome: {_clean(experience.outcome or '')}",
        "",
    ]
    seen_content: set[str] = set()
    content_line_count = 0
    for observation in sorted(experience.observations, key=lambda item: item.ordinal):
        parts = [f"Observation {observation.ordinal}"]
        uri = _observation_uri(observation)
        title = _page_title(observation)
        reasoning = _observation_reasoning(observation)
        if uri:
            parts.append(f"URI: {uri}")
        if title:
            parts.append(f"Page: {title}")
        if observation.action:
            parts.append(f"Action: {_clean(observation.action)}")
        if reasoning:
            parts.append(f"Reasoning: {reasoning}")
        lines.append(" | ".join(parts))
        for content_line in _salient_content_lines(
            observation,
            seen=seen_content,
            budget=MAX_CONTENT_LINES_TOTAL - content_line_count,
        ):
            lines.append(f"  · {content_line}")
            content_line_count += 1

    digest = "\n".join(lines)
    if len(digest) <= max_chars:
        return digest
    head_budget = int(max_chars * 0.7)
    tail_budget = max_chars - head_budget - 30
    return digest[:head_budget] + "\n[... digest truncated ...]\n" + digest[-tail_budget:]


def build_operational_note_distillation_prompt(digest: str) -> str:
    return _PROMPT_TEMPLATE.format(
        max_facts=MAX_FACT_ITEMS,
        max_gotchas=MAX_GOTCHA_ITEMS,
        digest=digest,
    )


def operational_note_distiller(
    *,
    agent: Agent[Any, Any] | None = None,
    max_tokens: int | None = 2_048,
) -> Extractor[DistilledOperationalNotes]:
    """Build the configured memory-surface extractor for operational notes."""
    return Extractor(
        DistilledOperationalNotes,
        surface=LLMSurface.MEMORY,
        system_prompt=OPERATIONAL_NOTE_DISTILLATION_SYSTEM_PROMPT,
        max_tokens=max_tokens,
        output_retries=2,
        agent=agent,
    )


def operational_distilled_note_id(source_id: str, note_kind: str) -> str:
    material = f"operational-distilled-note:{source_id}:{note_kind}"
    return f"note_{hashlib.sha256(material.encode()).hexdigest()[:16]}"


def build_operational_note_entities(
    notes: DistilledOperationalNotes,
    *,
    experience: OperationalExperience,
    organization_id: str,
    created_by: str | None,
    content_hash: str,
    provider: str | None = None,
    model: str | None = None,
) -> list[Entity]:
    """Project distilled notes into deterministic, replay-safe graph entities."""
    bodies: list[tuple[str, str]] = []
    if notes.workflow:
        bodies.append(("workflow", f"Distilled workflow:\n{notes.workflow}"))
    if notes.facts:
        bodies.append(
            (
                "facts",
                "Observed environment facts:\n" + "\n".join(f"- {fact}" for fact in notes.facts),
            )
        )
    if notes.gotchas:
        bodies.append(
            (
                "gotchas",
                "Observed gotchas:\n" + "\n".join(f"- {gotcha}" for gotcha in notes.gotchas),
            )
        )

    header = "\n".join(
        part
        for part in (
            f"Source: {experience.source_id}",
            f"Goal: {_clean(experience.goal)}",
            f"Outcome: {_clean(experience.outcome or '')}" if experience.outcome else None,
        )
        if part
    )
    entities: list[Entity] = []
    for note_kind, body in bodies:
        metadata: dict[str, Any] = {
            **experience.metadata,
            "category": OPERATIONAL_NOTE_CATEGORY,
            "operational_source_id": experience.source_id,
            "operational_content_hash": content_hash,
            "projection_kind": "distilled_note",
            "note_kind": note_kind,
            "note_distillation_schema": OPERATIONAL_NOTE_DISTILLATION_SCHEMA_VERSION,
        }
        if experience.project_id:
            metadata["project_id"] = experience.project_id
        if experience.scope_key:
            metadata["scope_key"] = experience.scope_key
        if provider:
            metadata["note_distillation_provider"] = provider
        if model:
            metadata["note_distillation_model"] = model
        entities.append(
            Entity(
                id=operational_distilled_note_id(experience.source_id, note_kind),
                entity_type=EntityType.NOTE,
                name=f"Distilled {note_kind} note for {experience.goal[:100]}",
                description=f"{experience.goal} ({experience.outcome or 'outcome not reported'})",
                content=f"{header}\n\n{body}"[:MAX_OPERATIONAL_NOTE_CHARS],
                organization_id=organization_id,
                created_by=created_by,
                modified_by=created_by,
                metadata=metadata,
            )
        )
    return entities


def _salient_content_lines(
    observation: OperationalObservation,
    *,
    seen: set[str],
    budget: int,
) -> list[str]:
    if budget <= 0:
        return []
    scored: list[tuple[int, str]] = []
    for tree in _observation_accessibility_trees(observation):
        for match in _CONTENT_NODE_RE.finditer(tree):
            role, name = match.group(1), _clean(match.group(2))
            weight = _CONTENT_ROLES.get(role)
            if weight is None or len(name) < MIN_CONTENT_NAME_CHARS:
                continue
            if not re.search(r"[A-Za-z0-9]{2}", name) or _CONTENT_NOISE_RE.search(name):
                continue
            key = f"{role}:{name}".casefold()
            if key in seen:
                continue
            seen.add(key)
            score = weight + (2 if any(character.isdigit() for character in name) else 0)
            scored.append((score, f"{role}: {name[:MAX_CONTENT_LINE_CHARS]}"))
    scored.sort(key=lambda row: -row[0])
    return [line for _score, line in scored[: min(MAX_CONTENT_LINES_PER_OBSERVATION, budget)]]


def _observation_accessibility_trees(observation: OperationalObservation) -> list[str]:
    trees = [
        evidence.content
        for evidence in observation.evidence
        if "accessibility-tree" in evidence.content_type.casefold()
    ]
    raw_tree = observation.metadata.get("accessibility_tree")
    if raw_tree:
        trees.append(str(raw_tree))
    return trees


def _observation_reasoning(observation: OperationalObservation) -> str:
    value = observation.reasoning or observation.metadata.get("thought") or ""
    return _clean(str(value))


def _observation_uri(observation: OperationalObservation) -> str:
    value = observation.uri or observation.metadata.get("url") or ""
    return _clean(str(value))


def _page_title(observation: OperationalObservation) -> str:
    for tree in _observation_accessibility_trees(observation):
        match = re.search(r"(?m)^\s*RootWebArea '([^']{1,120})'", tree)
        if match:
            return _clean(match.group(1))
    return _clean(str(observation.metadata.get("title") or ""))


def _clean(value: str) -> str:
    return " ".join(value.split())


__all__ = [
    "MAX_OPERATIONAL_NOTE_CHARS",
    "OPERATIONAL_NOTE_CATEGORY",
    "OPERATIONAL_NOTE_DISTILLATION_SCHEMA_VERSION",
    "DistilledOperationalNotes",
    "build_operational_experience_digest",
    "build_operational_note_distillation_prompt",
    "build_operational_note_entities",
    "operational_distilled_note_id",
    "operational_note_distiller",
]
