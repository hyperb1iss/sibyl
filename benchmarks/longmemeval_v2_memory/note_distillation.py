"""Ingest-time LLM note distillation for LongMemEval-V2 trajectories.

Distills each source trajectory into a small set of reusable typed notes
(workflow, facts, gotchas) from trajectory content alone — no question text
is ever involved, so distillation is a legitimate ingest-time projection.
The LLM call is pure (no shared state) so callers can run it on worker
threads and write the resulting payloads from their own thread.
"""

from __future__ import annotations

import json
import re
from typing import Any

NOTE_DISTILLATION_SCHEMA_VERSION = "sibyl-lme-note-distillation-v2"
DEFAULT_NOTE_DISTILLATION_MODEL = "gpt-5.4-nano"
MAX_DIGEST_CHARS = 40_000
MAX_NOTE_CHARS = 1_600
MAX_FACT_ITEMS = 10
MAX_GOTCHA_ITEMS = 5
MAX_CONTENT_LINES_PER_STATE = 8
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

NOTE_DISTILLATION_SYSTEM_PROMPT = (
    "You distill browser-agent trajectories into reusable operational memory "
    "notes for a future assistant working in the same environment. Write only "
    "what the trajectory itself evidences; never invent UI elements, labels, "
    "or outcomes. Prefer concrete names exactly as they appear (form names, "
    "field labels, menu paths, list columns, values)."
)

_PROMPT_TEMPLATE = """Distill the trajectory below into JSON with exactly these keys:
{{
  "workflow": "imperative, step-by-step recipe for the task that was performed \
(what to click/fill/navigate, in order), naming exact UI labels; empty string \
if the trajectory shows no coherent workflow",
  "facts": ["concrete, standalone environment facts observed (exact field \
labels, form/tab names, list columns, default values, locations of controls); \
each self-contained and specific"],
  "gotchas": ["pitfalls, errors, retries, or surprising behaviors observed, \
each with the condition that triggers it; empty list if none"]
}}

Limits: workflow <= 900 characters; at most {max_facts} facts; at most \
{max_gotchas} gotchas; each list item <= 300 characters.

Trajectory digest:
{digest}
"""


def build_trajectory_digest(
    trajectory: dict[str, Any],
    *,
    max_chars: int = MAX_DIGEST_CHARS,
) -> str:
    goal = _clean(str(trajectory.get("goal") or ""))
    outcome = _clean(str(trajectory.get("outcome") or ""))
    lines = [f"Goal: {goal}", f"Outcome: {outcome}", ""]
    seen_content: set[str] = set()
    content_line_count = 0
    states = trajectory.get("states")
    for index, state in enumerate(states if isinstance(states, list) else []):
        if not isinstance(state, dict):
            continue
        parts = [f"State {index}"]
        action = _clean(str(state.get("action") or ""))
        reasoning = _clean(str(state.get("reasoning") or state.get("thought") or ""))
        uri = _clean(str(state.get("uri") or state.get("url") or ""))
        title = _page_title(state)
        if uri:
            parts.append(f"URI: {uri}")
        if title:
            parts.append(f"Page: {title}")
        if action:
            parts.append(f"Action: {action}")
        if reasoning:
            parts.append(f"Reasoning: {reasoning}")
        lines.append(" | ".join(parts))
        for content_line in _salient_content_lines(
            state,
            seen=seen_content,
            budget=MAX_CONTENT_LINES_TOTAL - content_line_count,
        ):
            lines.append(f"  · {content_line}")
            content_line_count += 1
    digest = "\n".join(lines)
    if len(digest) > max_chars:
        head_budget = int(max_chars * 0.7)
        tail_budget = max_chars - head_budget - 30
        digest = digest[:head_budget] + "\n[... digest truncated ...]\n" + digest[-tail_budget:]
    return digest


def build_note_distillation_prompt(digest: str) -> str:
    return _PROMPT_TEMPLATE.format(
        max_facts=MAX_FACT_ITEMS,
        max_gotchas=MAX_GOTCHA_ITEMS,
        digest=digest,
    )


def parse_distillation_output(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise TypeError("distillation output must be a JSON object")
    workflow = str(payload.get("workflow") or "").strip()
    facts = [cleaned for value in (payload.get("facts") or []) if (cleaned := str(value).strip())][
        :MAX_FACT_ITEMS
    ]
    gotchas = [
        cleaned for value in (payload.get("gotchas") or []) if (cleaned := str(value).strip())
    ][:MAX_GOTCHA_ITEMS]
    if not workflow and not facts and not gotchas:
        raise ValueError("distillation output contained no notes")
    return {"workflow": workflow, "facts": facts, "gotchas": gotchas}


def distill_trajectory_notes(
    openai_client: Any,
    *,
    model: str,
    trajectory: dict[str, Any],
) -> dict[str, Any]:
    """Pure LLM call: safe to run on a worker thread. Raises on failure."""
    digest = build_trajectory_digest(trajectory)
    prompt = build_note_distillation_prompt(digest)
    last_error: Exception | None = None
    for _attempt in range(2):
        response = openai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": NOTE_DISTILLATION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or ""
        try:
            return parse_distillation_output(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
    raise RuntimeError(
        f"note distillation failed for trajectory {trajectory.get('id')!r}: {last_error}"
    )


def build_note_entity_payloads(
    notes: dict[str, Any],
    *,
    trajectory: dict[str, Any],
    project_id: str,
    run_id: str,
    model: str,
) -> list[dict[str, object]]:
    trajectory_id = str(trajectory.get("id") or "")
    goal = _clean(str(trajectory.get("goal") or ""))
    outcome = _clean(str(trajectory.get("outcome") or ""))
    header = f"Trajectory: {trajectory_id}\nGoal: {goal}\nOutcome: {outcome}"
    bodies: list[tuple[str, str]] = []
    if notes.get("workflow"):
        bodies.append(("workflow", f"Distilled workflow:\n{notes['workflow']}"))
    if notes.get("facts"):
        rendered = "\n".join(f"- {fact}" for fact in notes["facts"])
        bodies.append(("facts", f"Observed environment facts:\n{rendered}"))
    if notes.get("gotchas"):
        rendered = "\n".join(f"- {gotcha}" for gotcha in notes["gotchas"])
        bodies.append(("gotchas", f"Observed gotchas:\n{rendered}"))
    payloads: list[dict[str, object]] = []
    for kind, body in bodies:
        content = f"{header}\n\n{body}"
        payloads.append(
            {
                "name": f"Distilled {kind} note for trajectory {trajectory_id}",
                "description": f"{goal} ({outcome})",
                "content": content[:MAX_NOTE_CHARS],
                "entity_type": "note",
                "skip_conflicts": True,
                "metadata": {
                    "project_id": project_id,
                    "longmemeval_v2_run_id": run_id,
                    "longmemeval_v2_trajectory_id": trajectory_id,
                    "projection_kind": "distilled_note",
                    "note_kind": kind,
                    "note_distillation_schema": NOTE_DISTILLATION_SCHEMA_VERSION,
                    "note_distillation_model": model,
                },
            }
        )
    return payloads


def _salient_content_lines(
    state: dict[str, Any],
    *,
    seen: set[str],
    budget: int,
) -> list[str]:
    """Extract page-content lines (headings, cells, values) from a11y evidence.

    Web questions ask about page content, not workflow structure; without
    these lines the distiller cannot produce notes that carry content
    literals (the web-gate NO-GO mechanism).
    """
    if budget <= 0:
        return []
    scored: list[tuple[int, str]] = []
    for tree in _state_axtrees(state):
        for match in _CONTENT_NODE_RE.finditer(tree):
            role, name = match.group(1), _clean(match.group(2))
            weight = _CONTENT_ROLES.get(role)
            if weight is None or len(name) < MIN_CONTENT_NAME_CHARS:
                continue
            if not re.search(r"[A-Za-z0-9]{2}", name):
                continue
            if _CONTENT_NOISE_RE.search(name):
                continue
            key = f"{role}:{name}".casefold()
            if key in seen:
                continue
            score = weight + (2 if any(ch.isdigit() for ch in name) else 0)
            scored.append((score, f"{role}: {name[:MAX_CONTENT_LINE_CHARS]}"))
            seen.add(key)
    scored.sort(key=lambda row: -row[0])
    return [line for _score, line in scored[: min(MAX_CONTENT_LINES_PER_STATE, budget)]]


def _state_axtrees(state: dict[str, Any]) -> list[str]:
    trees: list[str] = []
    for evidence in state.get("evidence") or []:
        if not isinstance(evidence, dict):
            continue
        if "accessibility-tree" in str(evidence.get("content_type") or "").casefold():
            trees.append(str(evidence.get("content") or ""))
    raw_tree = state.get("accessibility_tree")
    if raw_tree:
        trees.append(str(raw_tree))
    return trees


def _clean(value: str) -> str:
    return " ".join(value.split())


def _page_title(state: dict[str, Any]) -> str:
    for tree in _state_axtrees(state):
        match = re.search(r"(?m)^\s*RootWebArea '([^']{1,120})'", tree)
        if match:
            return match.group(1)
    title = state.get("title")
    return _clean(str(title)) if title else ""
