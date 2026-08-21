"""LLM distillation of operational experience into reusable typed notes."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator
from pydantic_ai import Agent

from sibyl_core.ai.llm import Extractor, LLMSurface
from sibyl_core.auth.memory_policy import stamp_memory_scope_metadata
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
MAX_OBSERVED_ABSENCE_ITEMS = 5
OperationalNoteDistillationProfile = Literal["baseline", "render_v1"]

_CONTENT_NODE_RE = re.compile(r"(?:\[\w+\]\s+)?([A-Za-z]+)\s+'([^']{4,200})'")
_BASELINE_CONTENT_ROLES = {
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
RENDER_V1_CONTENT_ROLES = (
    "heading",
    "gridcell",
    "columnheader",
    "StaticText",
    "link",
    "option",
    "listitem",
)
_RENDER_V1_CONTENT_ROLES = {role: _BASELINE_CONTENT_ROLES[role] for role in RENDER_V1_CONTENT_ROLES}
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

_RENDER_V1_PROMPT_TEMPLATE = """Distill the trajectory below into exactly these fields:
- workflow: an imperative, step-by-step recipe for the performed task, naming exact UI labels;
  use an empty string when there is no coherent workflow
- facts: concrete, standalone environment facts with exact labels, names, values, and locations
- gotchas: pitfalls, errors, retries, or surprising behavior, each with its triggering condition
- observed_absence: objects with observation_ordinal and statement. Propose absence only when that
  exact observation is labeled Complete UI inventory; never infer absence from a Partial or empty
  inventory

Limits: workflow <= 900 characters; at most {max_facts} facts; at most {max_gotchas} gotchas;
at most {max_absences} observed absences; each fact, gotcha, or absence statement <= 300 characters.

Trajectory digest:
{digest}
"""


@dataclass(frozen=True, slots=True)
class _OperationalDistillationProfile:
    name: OperationalNoteDistillationProfile
    content_roles: dict[str, int]
    max_digest_chars: int = MAX_OPERATIONAL_DIGEST_CHARS
    max_note_chars: int = MAX_OPERATIONAL_NOTE_CHARS
    max_lines_per_observation: int = MAX_CONTENT_LINES_PER_OBSERVATION
    max_lines_total: int = MAX_CONTENT_LINES_TOTAL
    max_line_chars: int = MAX_CONTENT_LINE_CHARS


_DISTILLATION_PROFILES = {
    "baseline": _OperationalDistillationProfile(
        name="baseline",
        content_roles=_BASELINE_CONTENT_ROLES,
    ),
    "render_v1": _OperationalDistillationProfile(
        name="render_v1",
        content_roles=_RENDER_V1_CONTENT_ROLES,
    ),
}

OperationalNoteItem = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]


class ObservedOperationalAbsence(BaseModel):
    """One model-proposed absence tied to an exact source observation."""

    observation_ordinal: int = Field(ge=0)
    statement: OperationalNoteItem


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


class RenderV1DistilledOperationalNotes(BaseModel):
    """Treatment payload with ordinal-scoped observed-absence proposals."""

    workflow: str = Field(default="", max_length=900)
    facts: list[OperationalNoteItem] = Field(default_factory=list, max_length=MAX_FACT_ITEMS)
    gotchas: list[OperationalNoteItem] = Field(default_factory=list, max_length=MAX_GOTCHA_ITEMS)
    observed_absence: list[ObservedOperationalAbsence] = Field(
        default_factory=list,
        max_length=MAX_OBSERVED_ABSENCE_ITEMS,
    )

    @model_validator(mode="after")
    def require_note_content(self) -> RenderV1DistilledOperationalNotes:
        self.workflow = self.workflow.strip()
        if not self.workflow and not self.facts and not self.gotchas and not self.observed_absence:
            raise ValueError("distillation output contained no notes")
        return self


OperationalDistillationOutput = DistilledOperationalNotes | RenderV1DistilledOperationalNotes


def build_operational_experience_digest(
    experience: OperationalExperience,
    *,
    max_chars: int = MAX_OPERATIONAL_DIGEST_CHARS,
    profile: OperationalNoteDistillationProfile = "baseline",
) -> str:
    """Render bounded source evidence for the distillation model."""
    digest, _receipt = build_operational_experience_digest_with_receipt(
        experience,
        max_chars=max_chars,
        profile=profile,
    )
    return digest


def build_operational_experience_digest_with_receipt(
    experience: OperationalExperience,
    *,
    max_chars: int = MAX_OPERATIONAL_DIGEST_CHARS,
    profile: OperationalNoteDistillationProfile = "baseline",
) -> tuple[str, dict[str, Any]]:
    """Render a digest and explain its role, line, character, and inventory bounds."""
    selected_profile = _distillation_profile(profile)
    if profile == "render_v1" and max_chars < 1:
        raise ValueError("render_v1 max_chars must be positive")
    lines = [
        f"Goal: {_clean(experience.goal)}",
        f"Outcome: {_clean(experience.outcome or '')}",
        "",
    ]
    seen_content: set[str] = set()
    content_line_count = 0
    content_candidate_count = 0
    content_role_counts: Counter[str] = Counter()
    inventories: list[dict[str, Any]] = []
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
        observation_seen = seen_content if profile == "baseline" else set()
        content_lines, inventory = _salient_content_lines_with_receipt(
            observation,
            seen=observation_seen,
            budget=selected_profile.max_lines_total - content_line_count,
            profile=selected_profile,
        )
        inventories.append(inventory)
        content_candidate_count += int(inventory["candidate_line_count"])
        content_role_counts.update(inventory["candidate_role_counts"])
        if profile == "render_v1" and int(inventory["accessibility_tree_count"]) > 0:
            inventory_label = "Complete" if inventory["complete"] else "Partial"
            lines.append(
                f"  {inventory_label} UI inventory for observation {observation.ordinal} "
                f"({inventory['admitted_line_count']} eligible elements):"
            )
        for content_line in content_lines:
            lines.append(f"  · {content_line}")
            content_line_count += 1

    unbounded_digest = "\n".join(lines)
    digest_truncated = len(unbounded_digest) > max_chars
    digest = unbounded_digest
    if digest_truncated:
        head_budget = int(max_chars * 0.7)
        tail_budget = max_chars - head_budget - 30
        digest = digest[:head_budget] + "\n[... digest truncated ...]\n" + digest[-tail_budget:]
        for inventory in inventories:
            if inventory["complete"]:
                inventory["complete"] = False
                inventory["truncated"] = True
                inventory["rejection_reasons"].append("digest_truncated")
    return digest, {
        "profile": profile,
        "roles": list(selected_profile.content_roles),
        "candidate_line_count": content_candidate_count,
        "admitted_line_count": content_line_count,
        "candidate_role_counts": dict(sorted(content_role_counts.items())),
        "content_chars": sum(len(line) for line in lines[3:]),
        "digest_chars": len(digest),
        "unbounded_digest_chars": len(unbounded_digest),
        "max_digest_chars": max_chars,
        "max_lines_per_observation": selected_profile.max_lines_per_observation,
        "max_lines_total": selected_profile.max_lines_total,
        "max_line_chars": selected_profile.max_line_chars,
        "configured_budget": {
            "digest_chars": max_chars,
            "lines_per_observation": selected_profile.max_lines_per_observation,
            "lines_total": selected_profile.max_lines_total,
            "line_chars": selected_profile.max_line_chars,
        },
        "within_digest_char_budget": len(digest) <= max_chars,
        "within_line_budget": (
            content_line_count <= selected_profile.max_lines_total
            and all(
                int(inventory["admitted_line_count"]) <= selected_profile.max_lines_per_observation
                for inventory in inventories
            )
        ),
        "truncated": digest_truncated or any(inventory["truncated"] for inventory in inventories),
        "digest_truncated": digest_truncated,
        "inventories": inventories,
    }


def build_operational_note_distillation_prompt(
    digest: str,
    *,
    profile: OperationalNoteDistillationProfile = "baseline",
) -> str:
    _distillation_profile(profile)
    template = _PROMPT_TEMPLATE if profile == "baseline" else _RENDER_V1_PROMPT_TEMPLATE
    return template.format(
        max_facts=MAX_FACT_ITEMS,
        max_gotchas=MAX_GOTCHA_ITEMS,
        max_absences=MAX_OBSERVED_ABSENCE_ITEMS,
        digest=digest,
    )


def operational_note_distiller(
    *,
    agent: Agent[Any, Any] | None = None,
    max_tokens: int | None = 2_048,
    profile: OperationalNoteDistillationProfile = "baseline",
) -> Extractor[DistilledOperationalNotes] | Extractor[RenderV1DistilledOperationalNotes]:
    """Build the configured memory-surface extractor for operational notes."""
    _distillation_profile(profile)
    system_prompt = OPERATIONAL_NOTE_DISTILLATION_SYSTEM_PROMPT
    if profile == "render_v1":
        system_prompt += (
            " Treat absence as evidence only for an explicitly complete inventory at the "
            "same observation ordinal."
        )
    output_type = (
        DistilledOperationalNotes if profile == "baseline" else RenderV1DistilledOperationalNotes
    )
    return Extractor(
        output_type,
        surface=LLMSurface.MEMORY,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        output_retries=2,
        agent=agent,
    )


def operational_distilled_note_id(source_id: str, note_kind: str) -> str:
    material = f"operational-distilled-note:{source_id}:{note_kind}"
    return f"note_{hashlib.sha256(material.encode()).hexdigest()[:16]}"


def build_operational_note_entities(
    notes: OperationalDistillationOutput,
    *,
    experience: OperationalExperience,
    organization_id: str,
    created_by: str | None,
    content_hash: str,
    provider: str | None = None,
    model: str | None = None,
    profile: OperationalNoteDistillationProfile = "baseline",
    admitted_observed_absence: list[ObservedOperationalAbsence] | None = None,
) -> list[Entity]:
    """Project distilled notes into deterministic, replay-safe graph entities."""
    entities, _receipt = build_operational_note_entities_with_receipt(
        notes,
        experience=experience,
        organization_id=organization_id,
        created_by=created_by,
        content_hash=content_hash,
        provider=provider,
        model=model,
        profile=profile,
        admitted_observed_absence=admitted_observed_absence,
    )
    return entities


def build_operational_note_entities_with_receipt(
    notes: OperationalDistillationOutput,
    *,
    experience: OperationalExperience,
    organization_id: str,
    created_by: str | None,
    content_hash: str,
    provider: str | None = None,
    model: str | None = None,
    profile: OperationalNoteDistillationProfile = "baseline",
    admitted_observed_absence: list[ObservedOperationalAbsence] | None = None,
) -> tuple[list[Entity], dict[str, Any]]:
    """Project notes and return exact rendered-size receipts."""
    selected_profile = _distillation_profile(profile)
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
    admitted_absence = admitted_observed_absence or []
    if admitted_absence:
        bodies.append(
            (
                "observed_absence",
                "Observed absence from complete UI inventories:\n"
                + "\n".join(
                    f"- Observation {item.observation_ordinal}: {item.statement}"
                    for item in admitted_absence
                ),
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
    rendered_notes: list[dict[str, Any]] = []
    for note_kind, body in bodies:
        metadata: dict[str, Any] = {
            # Same untrusted capture bag the projection sees, reaching the graph
            # through the distillation job instead.
            **stamp_memory_scope_metadata(
                experience.metadata,
                memory_scope=None,
                scope_key=None,
                principal_id=None,
            ),
            "category": OPERATIONAL_NOTE_CATEGORY,
            "operational_source_id": experience.source_id,
            "operational_content_hash": content_hash,
            "projection_kind": "distilled_note",
            "note_kind": note_kind,
            "note_distillation_schema": (
                OPERATIONAL_NOTE_DISTILLATION_SCHEMA_VERSION
                if profile == "baseline"
                else "sibyl-operational-note-distillation-render-v1"
            ),
        }
        if experience.project_id:
            metadata["project_id"] = experience.project_id
            metadata["scope_key"] = experience.project_id
        if provider:
            metadata["note_distillation_provider"] = provider
        if model:
            metadata["note_distillation_model"] = model
        if profile != "baseline":
            metadata["operational_note_distillation_profile"] = profile
        full_content = f"{header}\n\n{body}"
        content = full_content[: selected_profile.max_note_chars]
        rendered_notes.append(
            {
                "note_kind": note_kind,
                "lines": content.count("\n") + 1,
                "chars": len(content),
                "unbounded_chars": len(full_content),
                "truncated": len(content) < len(full_content),
            }
        )
        entities.append(
            Entity(
                id=operational_distilled_note_id(experience.source_id, note_kind),
                entity_type=EntityType.NOTE,
                name=f"Distilled {note_kind} note for {experience.goal[:100]}",
                description=f"{experience.goal} ({experience.outcome or 'outcome not reported'})",
                content=content,
                organization_id=organization_id,
                created_by=created_by,
                modified_by=created_by,
                metadata=metadata,
            )
        )
    return entities, {
        "profile": profile,
        "note_count": len(rendered_notes),
        "lines": sum(int(note["lines"]) for note in rendered_notes),
        "chars": sum(int(note["chars"]) for note in rendered_notes),
        "truncated": any(bool(note["truncated"]) for note in rendered_notes),
        "max_note_chars": selected_profile.max_note_chars,
        "within_note_char_budget": all(
            int(note["chars"]) <= selected_profile.max_note_chars for note in rendered_notes
        ),
        "notes": rendered_notes,
    }


def admit_observed_operational_absence(
    notes: OperationalDistillationOutput,
    *,
    digest_receipt: dict[str, Any],
    profile: OperationalNoteDistillationProfile,
) -> tuple[list[ObservedOperationalAbsence], dict[str, Any]]:
    """Admit absence only against the exact complete inventory the model read."""
    _distillation_profile(profile)
    inventory_by_ordinal = {
        inventory["observation_ordinal"]: inventory
        for inventory in digest_receipt.get("inventories", [])
        if isinstance(inventory, dict)
    }
    admitted: list[ObservedOperationalAbsence] = []
    activity: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    proposals = (
        notes.observed_absence if isinstance(notes, RenderV1DistilledOperationalNotes) else []
    )
    for index, proposal in enumerate(proposals):
        key = (proposal.observation_ordinal, proposal.statement.casefold())
        inventory = inventory_by_ordinal.get(proposal.observation_ordinal)
        if profile != "render_v1":
            reason = "profile_not_enabled"
        elif key in seen:
            reason = "duplicate_proposal"
        elif inventory is None:
            reason = "observation_not_found"
        elif not inventory.get("accessibility_tree_count"):
            reason = "inventory_missing"
        elif not inventory.get("candidate_line_count"):
            reason = "inventory_empty"
        elif inventory.get("truncated") or not inventory.get("complete"):
            reasons = inventory.get("rejection_reasons") or []
            reason = str(reasons[0]) if reasons else "inventory_truncated"
        else:
            reason = "complete_inventory"
            admitted.append(proposal)
            seen.add(key)
        activity.append(
            {
                "index": index,
                "observation_ordinal": proposal.observation_ordinal,
                "statement": proposal.statement,
                "status": "admitted" if reason == "complete_inventory" else "rejected",
                "reason": reason,
                "inventory_complete": bool(
                    isinstance(inventory, dict)
                    and inventory.get("complete")
                    and not inventory.get("truncated")
                ),
                "inventory_rejection_reasons": (
                    list(inventory.get("rejection_reasons") or [])
                    if isinstance(inventory, dict)
                    else []
                ),
            }
        )
    return admitted, {
        "proposed_count": len(proposals),
        "admitted_count": len(admitted),
        "rejected_count": len(proposals) - len(admitted),
        "proposals": activity,
    }


def _salient_content_lines(
    observation: OperationalObservation,
    *,
    seen: set[str],
    budget: int,
) -> list[str]:
    lines, _receipt = _salient_content_lines_with_receipt(
        observation,
        seen=seen,
        budget=budget,
        profile=_DISTILLATION_PROFILES["baseline"],
    )
    return lines


def _salient_content_lines_with_receipt(
    observation: OperationalObservation,
    *,
    seen: set[str],
    budget: int,
    profile: _OperationalDistillationProfile,
) -> tuple[list[str], dict[str, Any]]:
    scored: list[tuple[int, str, str, bool]] = []
    tree_count = 0
    for tree in _observation_accessibility_trees(observation):
        tree_count += 1
        for match in _CONTENT_NODE_RE.finditer(tree):
            role, name = match.group(1), _clean(match.group(2))
            weight = profile.content_roles.get(role)
            if weight is None or len(name) < MIN_CONTENT_NAME_CHARS:
                continue
            if not re.search(r"[A-Za-z0-9]{2}", name) or _CONTENT_NOISE_RE.search(name):
                continue
            key = f"{role}:{name}".casefold()
            if key in seen:
                continue
            seen.add(key)
            score = weight + (2 if any(character.isdigit() for character in name) else 0)
            clipped = len(name) > profile.max_line_chars
            line = f"{role}: {name[: profile.max_line_chars]}"
            scored.append((score, role, line, clipped))
    scored.sort(key=lambda row: -row[0])
    admitted_limit = min(profile.max_lines_per_observation, max(0, budget))
    admitted = scored[:admitted_limit]
    candidate_count = len(scored)
    admitted_count = len(admitted)
    rejection_reasons: list[str] = []
    if candidate_count > profile.max_lines_per_observation:
        rejection_reasons.append("observation_line_budget")
    if candidate_count > admitted_count and budget < profile.max_lines_per_observation:
        rejection_reasons.append("total_line_budget")
    if any(clipped for _score, _role, _line, clipped in admitted):
        rejection_reasons.append("line_char_budget")
    truncated = bool(rejection_reasons)
    return [line for _score, _role, line, _clipped in admitted], {
        "observation_ordinal": observation.ordinal,
        "accessibility_tree_count": tree_count,
        "candidate_line_count": candidate_count,
        "admitted_line_count": admitted_count,
        "candidate_role_counts": dict(
            sorted(Counter(role for _score, role, _line, _clipped in scored).items())
        ),
        "complete": tree_count > 0 and candidate_count > 0 and not truncated,
        "truncated": truncated,
        "rejection_reasons": rejection_reasons,
    }


def _distillation_profile(
    profile: OperationalNoteDistillationProfile,
) -> _OperationalDistillationProfile:
    try:
        return _DISTILLATION_PROFILES[profile]
    except KeyError as exc:
        raise ValueError(
            "operational_note_distillation_profile must be baseline or render_v1"
        ) from exc


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
    "RENDER_V1_CONTENT_ROLES",
    "DistilledOperationalNotes",
    "ObservedOperationalAbsence",
    "OperationalDistillationOutput",
    "OperationalNoteDistillationProfile",
    "RenderV1DistilledOperationalNotes",
    "admit_observed_operational_absence",
    "build_operational_experience_digest",
    "build_operational_experience_digest_with_receipt",
    "build_operational_note_distillation_prompt",
    "build_operational_note_entities",
    "build_operational_note_entities_with_receipt",
    "operational_distilled_note_id",
    "operational_note_distiller",
]
