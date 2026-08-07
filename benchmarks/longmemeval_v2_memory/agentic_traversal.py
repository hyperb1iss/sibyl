"""Bounded model-in-the-loop traversal over the slice substrate (A3).

The one-shot pass retrieves a pool and the deterministic composer renders it.
This controller adds the step between them: a model reads the question and the
pool's previews, decides whether the evidence gap is closed, and when it is not
issues bounded gather actions against the client-exposed traversal verbs
(`search`, `expand_neighbors`, `fetch_slice`). Gathered candidates join the
composer's admission through their own granted lane, so the model chooses what
to gather and never how the pack renders.

Stitch geometry cannot cross the retrieval pool boundary: every ring-major
spread pack held state recall exactly at the additive arm's number because no
stitch reaches a state outside the pool. Traversal is the mechanism that can
cross it, which is why the loop exists at all.

Bounds are structural rather than advisory. The loop runs at most
MAX_WIDENING_ROUNDS widening rounds on top of the seed pass (three rounds
total, matching the verbs' documented round budget), each round issues at most
`max_actions` verb calls, one follow-up search is allowed per question, and a
wall-clock deadline stops gathering before the pack's latency budget is spent.
Every hook is injected so the loop is testable without a network or a model:
the adapter wires REST calls and an OpenAI client in production.

The controller is score-blind and question-text-only: the prompt carries the
question text and retrieval previews, never gold answers or scorer state.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# The seed pass is round one of the verbs' documented three-round budget, so
# the model may widen at most twice on top of it.
MAX_WIDENING_ROUNDS = 2
DEFAULT_WIDENING_ROUNDS = 1

DEFAULT_MAX_ACTIONS_PER_ROUND = 4
MAX_ACTIONS_PER_ROUND = 8

DEFAULT_FOLLOWUP_SEARCHES = 1
MAX_FOLLOWUP_SEARCHES = 1

# The fast one-shot pass averages ~7.5s and the gate's ceiling is p95 <= 15s,
# so gathering gets the difference, not a budget of its own.
DEFAULT_TRAVERSAL_DEADLINE_SECONDS = 8.0

DEFAULT_POOL_PREVIEW_ITEMS = 24
DEFAULT_POOL_PREVIEW_CHARS = 200

TRAVERSAL_ORIGIN_EXPAND = "traversal:expand"
TRAVERSAL_ORIGIN_SLICE = "traversal:slice"
TRAVERSAL_ORIGIN_SEARCH = "traversal:search"

_EXPAND_VERB = "expand_neighbors"
_SLICE_VERB = "fetch_slice"
_SEARCH_VERB = "search"
_KNOWN_VERBS = frozenset({_EXPAND_VERB, _SLICE_VERB, _SEARCH_VERB})

TRAVERSAL_SYSTEM_PROMPT = (
    "You steer memory retrieval for a question-answering system. You never "
    "answer the question yourself. You inspect the evidence pool gathered so "
    "far and either declare it sufficient or request bounded follow-up "
    "retrieval. Respond with a single JSON object and nothing else."
)


@dataclass
class TraversalAction:
    """One verb call the model requested, already validated against the caps."""

    verb: str
    entity_ids: list[str] = field(default_factory=list)
    entity_id: str = ""
    query: str = ""
    relationship_types: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)


@dataclass
class TraversalDecision:
    """The model's verdict for one round, after defensive parsing."""

    sufficient: bool
    actions: list[TraversalAction] = field(default_factory=list)
    dropped_actions: list[str] = field(default_factory=list)
    parse_error: str = ""


def _stripped(value: object) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [cleaned for item in value if (cleaned := _stripped(item))]


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def candidate_preview_line(rank: int, candidate: dict[str, object], *, preview_chars: int) -> str:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    trajectory_id = _stripped(metadata.get("longmemeval_v2_trajectory_id")) or "unknown"
    content = _stripped(candidate.get("content")).replace("\n", " ")
    if len(content) > preview_chars:
        content = content[:preview_chars].rstrip() + "..."
    return (
        f"{rank}. id={_stripped(candidate.get('id')) or 'unknown'}"
        f" type={_stripped(candidate.get('type')) or 'unknown'}"
        f" trajectory={trajectory_id}"
        f" name={_stripped(candidate.get('name'))[:80]}"
        f" :: {content}"
    )


def build_pool_previews(
    candidates: Sequence[dict[str, object]],
    *,
    max_items: int = DEFAULT_POOL_PREVIEW_ITEMS,
    preview_chars: int = DEFAULT_POOL_PREVIEW_CHARS,
) -> list[str]:
    return [
        candidate_preview_line(rank, candidate, preview_chars=preview_chars)
        for rank, candidate in enumerate(candidates[:max_items], start=1)
    ]


def build_traversal_prompt(
    *,
    question: str,
    pool_previews: Sequence[str],
    gathered_previews: Sequence[str],
    round_number: int,
    total_rounds: int,
    max_actions: int,
    followup_searches_left: int,
) -> str:
    lines = [
        f"Retrieval round {round_number} of {total_rounds}.",
        "",
        "Question (do not answer it; gather evidence for it):",
        question.strip(),
        "",
        "Evidence pool previews (rank, id, type, trajectory, name, content):",
        *(pool_previews or ["(empty pool)"]),
    ]
    if gathered_previews:
        lines.extend(
            [
                "",
                "Already gathered by earlier rounds (do not fetch these again):",
                *gathered_previews,
            ]
        )
    search_rule = (
        f'- at most {followup_searches_left} additional {{"verb": "search"}} action'
        if followup_searches_left > 0
        else '- the "search" verb is exhausted for this question; do not request it'
    )
    lines.extend(
        [
            "",
            "Decide whether this pool already carries the evidence the question",
            "needs. A single-hop question answerable from the pool should stop",
            "here. Multi-hop, associative, and temporal gaps are what widening",
            "is for.",
            "",
            "Respond with strict JSON, one object, no prose:",
            '  {"sufficient": true}',
            "or",
            '  {"sufficient": false, "actions": [',
            '    {"verb": "expand_neighbors", "entity_ids": ["<pool id>", "..."],',
            '     "relationship_types": []},',
            '    {"verb": "fetch_slice", "entity_id": "<pool or neighbor id>"},',
            '    {"verb": "search", "query": "<refined query>"}',
            "  ]}",
            "",
            "Rules:",
            f"- at most {max_actions} actions this round",
            "- expand_neighbors entity_ids must come from the previews above",
            "- fetch_slice widens one id from the previews into its span window",
            search_rule,
            "- request only what closes the evidence gap; fewer actions is better",
        ]
    )
    return "\n".join(lines)


def parse_traversal_decision(
    raw: str,
    *,
    max_actions: int,
    known_ids: set[str],
    followup_searches_left: int,
) -> TraversalDecision:
    """Parse the model's round verdict, dropping anything outside the caps.

    A malformed reply is treated as "sufficient": the arm degrades to the
    one-shot baseline rather than crashing a scored run, and the drop is
    recorded so the trace shows the round was lost to parsing.
    """
    try:
        payload = json.loads(_strip_code_fence(raw))
    except (TypeError, ValueError) as exc:
        return TraversalDecision(sufficient=True, parse_error=f"invalid JSON: {exc}")
    if not isinstance(payload, dict):
        return TraversalDecision(sufficient=True, parse_error="decision is not a JSON object")
    if payload.get("sufficient"):
        return TraversalDecision(sufficient=True)

    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        return TraversalDecision(sufficient=True, parse_error="insufficient without actions")

    actions: list[TraversalAction] = []
    dropped: list[str] = []
    searches_left = followup_searches_left
    for entry in raw_actions:
        if len(actions) >= max_actions:
            dropped.append("action cap reached")
            break
        action, drop_reason, searches_left = _parse_action(
            entry,
            known_ids=known_ids,
            searches_left=searches_left,
        )
        if action is None:
            dropped.append(drop_reason)
        else:
            actions.append(action)
    if not actions:
        return TraversalDecision(
            sufficient=True,
            dropped_actions=dropped,
            parse_error="no executable actions survived validation",
        )
    return TraversalDecision(sufficient=False, actions=actions, dropped_actions=dropped)


def _parse_action(
    entry: object,
    *,
    known_ids: set[str],
    searches_left: int,
) -> tuple[TraversalAction | None, str, int]:
    """Validate one requested action, returning (action, drop reason, budget)."""
    if not isinstance(entry, dict):
        return None, "action is not an object", searches_left
    verb = _stripped(entry.get("verb"))
    if verb not in _KNOWN_VERBS:
        return None, f"unknown verb {verb or '(empty)'!r}", searches_left
    if verb == _EXPAND_VERB:
        entity_ids = [
            entity_id
            for entity_id in _string_list(entry.get("entity_ids"))
            if entity_id in known_ids
        ]
        if not entity_ids:
            return None, "expand_neighbors without known entity_ids", searches_left
        action = TraversalAction(
            verb=verb,
            entity_ids=entity_ids,
            relationship_types=_string_list(entry.get("relationship_types")),
            types=_string_list(entry.get("types")),
        )
        return action, "", searches_left
    if verb == _SLICE_VERB:
        entity_id = _stripped(entry.get("entity_id"))
        if entity_id not in known_ids:
            return None, "fetch_slice without a known entity_id", searches_left
        return TraversalAction(verb=verb, entity_id=entity_id), "", searches_left
    query = _stripped(entry.get("query"))
    if not query:
        return None, "search without a query", searches_left
    if searches_left <= 0:
        return None, "search budget exhausted", searches_left
    return TraversalAction(verb=verb, query=query), "", searches_left - 1


def neighbors_to_candidates(response: dict[str, object]) -> list[dict[str, object]]:
    """Map an expand_neighbors response onto evidence-candidate dicts."""
    neighbors = response.get("neighbors")
    if not isinstance(neighbors, list):
        return []
    candidates: list[dict[str, object]] = []
    for neighbor in neighbors:
        if not isinstance(neighbor, dict):
            continue
        neighbor_id = _stripped(neighbor.get("id"))
        if not neighbor_id:
            continue
        neighbor_metadata = (
            dict(neighbor["metadata"]) if isinstance(neighbor.get("metadata"), dict) else {}
        )
        metadata: dict[str, object] = {
            "traversal_verb": _EXPAND_VERB,
            "traversal_relationship": _stripped(neighbor.get("relationship")),
            "traversal_direction": _stripped(neighbor.get("direction")),
            "traversal_distance": neighbor.get("distance"),
            **neighbor_metadata,
        }
        candidates.append(
            {
                "id": neighbor_id,
                "type": _stripped(neighbor.get("type")) or "entity",
                "name": _stripped(neighbor.get("name")),
                "content": _stripped(neighbor.get("content")),
                "score": neighbor.get("score"),
                "metadata": metadata,
                "_selection_origin": TRAVERSAL_ORIGIN_EXPAND,
            }
        )
    return candidates


def slice_to_candidate(response: dict[str, object]) -> dict[str, object] | None:
    """Map a fetch_slice response onto one evidence-candidate dict.

    The window renders as one candidate rather than one per span: spans are
    adjacent by construction and splitting them would spend admission slots on
    fragments of a single read. The candidate id carries the window position so
    it can never collide with the parent entity already sitting in the pool.
    """
    passages = response.get("passages")
    if not isinstance(passages, list) or not passages:
        return None
    parent_id = _stripped(response.get("parent_id"))
    if not parent_id:
        return None
    parts: list[str] = []
    for passage in passages:
        if not isinstance(passage, dict):
            continue
        content = _stripped(passage.get("content"))
        if not content:
            continue
        breadcrumb = _stripped(passage.get("breadcrumb"))
        parts.append(f"[{breadcrumb}]\n{content}" if breadcrumb else content)
    if not parts:
        return None
    window_start = response.get("window_start")
    return {
        "id": f"{parent_id}#slice:{window_start if window_start is not None else 'whole'}",
        "type": _stripped(response.get("parent_type")) or "entity",
        "name": _stripped(response.get("parent_name")),
        "content": "\n\n".join(parts),
        "score": None,
        "metadata": {
            "traversal_verb": _SLICE_VERB,
            "parent_entity_id": parent_id,
            "window_start": window_start,
            "passage_total": response.get("passage_total"),
            "covers_parent": response.get("covers_parent"),
            "sliced": response.get("sliced"),
        },
        "_selection_origin": TRAVERSAL_ORIGIN_SLICE,
    }


def run_agentic_traversal(
    *,
    question: str,
    pool: Sequence[dict[str, object]],
    llm_complete: Callable[[str, str], str],
    execute_expand: Callable[[TraversalAction], dict[str, object]],
    execute_slice: Callable[[TraversalAction], dict[str, object]],
    execute_search: Callable[[TraversalAction], list[dict[str, object]]],
    widening_rounds: int = DEFAULT_WIDENING_ROUNDS,
    max_actions: int = DEFAULT_MAX_ACTIONS_PER_ROUND,
    followup_searches: int = DEFAULT_FOLLOWUP_SEARCHES,
    deadline_seconds: float = DEFAULT_TRAVERSAL_DEADLINE_SECONDS,
    preview_items: int = DEFAULT_POOL_PREVIEW_ITEMS,
    preview_chars: int = DEFAULT_POOL_PREVIEW_CHARS,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Run the bounded widening loop and return (gathered candidates, trace).

    Each verb call is isolated: one failed action is recorded in the trace and
    the round continues, so a flaky expansion can cost its own yield and
    nothing else. Candidates are deduplicated against the pool and against
    earlier rounds by id.
    """
    widening_rounds = max(0, min(int(widening_rounds), MAX_WIDENING_ROUNDS))
    max_actions = max(1, min(int(max_actions), MAX_ACTIONS_PER_ROUND))
    followup_searches_left = max(0, min(int(followup_searches), MAX_FOLLOWUP_SEARCHES))
    started_at = clock()
    deadline_at = started_at + max(0.0, float(deadline_seconds))

    gathered: list[dict[str, object]] = []
    seen_ids = {
        candidate_id for candidate in pool if (candidate_id := _stripped(candidate.get("id")))
    }
    pool_previews = build_pool_previews(pool, max_items=preview_items, preview_chars=preview_chars)
    trace: dict[str, object] = {
        "enabled": True,
        "rounds_budget": widening_rounds,
        "rounds_used": 0,
        "sufficient_early_stop": False,
        "deadline_hit": False,
        "actions": [],
        "parse_errors": [],
        "dropped_actions": [],
        "followup_searches_used": 0,
    }
    action_traces = trace["actions"]
    parse_errors = trace["parse_errors"]
    dropped_actions = trace["dropped_actions"]
    assert isinstance(action_traces, list)
    assert isinstance(parse_errors, list)
    assert isinstance(dropped_actions, list)

    total_rounds = widening_rounds + 1
    for round_number in range(2, total_rounds + 1):
        if clock() >= deadline_at:
            trace["deadline_hit"] = True
            break
        prompt = build_traversal_prompt(
            question=question,
            pool_previews=pool_previews,
            gathered_previews=build_pool_previews(
                gathered,
                max_items=preview_items,
                preview_chars=preview_chars,
            ),
            round_number=round_number,
            total_rounds=total_rounds,
            max_actions=max_actions,
            followup_searches_left=followup_searches_left,
        )
        try:
            raw_decision = llm_complete(TRAVERSAL_SYSTEM_PROMPT, prompt)
        except Exception as exc:  # one lost round, not a lost run
            parse_errors.append(f"round {round_number}: llm call failed: {exc}")
            break
        decision = parse_traversal_decision(
            raw_decision,
            max_actions=max_actions,
            known_ids=seen_ids,
            followup_searches_left=followup_searches_left,
        )
        trace["rounds_used"] = round_number - 1
        if decision.parse_error:
            parse_errors.append(f"round {round_number}: {decision.parse_error}")
        dropped_actions.extend(decision.dropped_actions)
        if decision.sufficient:
            trace["sufficient_early_stop"] = not decision.parse_error
            break

        for action in decision.actions:
            if clock() >= deadline_at:
                trace["deadline_hit"] = True
                break
            if action.verb == _SEARCH_VERB:
                followup_searches_left -= 1
                trace["followup_searches_used"] = int(trace.get("followup_searches_used", 0)) + 1
            new_candidates, action_trace = _execute_action(
                action,
                execute_expand=execute_expand,
                execute_slice=execute_slice,
                execute_search=execute_search,
            )
            admitted = 0
            for candidate in new_candidates:
                candidate_id = _stripped(candidate.get("id"))
                if candidate_id and candidate_id in seen_ids:
                    continue
                if candidate_id:
                    seen_ids.add(candidate_id)
                gathered.append(candidate)
                admitted += 1
            action_trace["gathered"] = admitted
            action_traces.append(action_trace)
        if trace["deadline_hit"]:
            break

    trace["gathered_candidates"] = len(gathered)
    trace["elapsed_seconds"] = round(clock() - started_at, 3)
    return gathered, trace


def _execute_action(
    action: TraversalAction,
    *,
    execute_expand: Callable[[TraversalAction], dict[str, object]],
    execute_slice: Callable[[TraversalAction], dict[str, object]],
    execute_search: Callable[[TraversalAction], list[dict[str, object]]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Run one verb call in isolation, returning its candidates and trace row.

    A failed call costs its own yield and nothing else: the error lands in the
    trace row and the round continues with the remaining actions.
    """
    action_trace: dict[str, object] = {"verb": action.verb, "ok": True, "gathered": 0}
    try:
        if action.verb == _EXPAND_VERB:
            action_trace["entity_ids"] = list(action.entity_ids)
            return neighbors_to_candidates(execute_expand(action)), action_trace
        if action.verb == _SLICE_VERB:
            action_trace["entity_id"] = action.entity_id
            candidate = slice_to_candidate(execute_slice(action))
            return ([candidate] if candidate else []), action_trace
        action_trace["query"] = action.query
        return [
            dict(candidate, _selection_origin=TRAVERSAL_ORIGIN_SEARCH)
            for candidate in execute_search(action)
            if isinstance(candidate, dict)
        ], action_trace
    except Exception as exc:  # isolate one verb call
        action_trace["ok"] = False
        action_trace["error"] = str(exc)
        return [], action_trace


__all__ = [
    "DEFAULT_FOLLOWUP_SEARCHES",
    "DEFAULT_MAX_ACTIONS_PER_ROUND",
    "DEFAULT_TRAVERSAL_DEADLINE_SECONDS",
    "DEFAULT_WIDENING_ROUNDS",
    "MAX_ACTIONS_PER_ROUND",
    "MAX_FOLLOWUP_SEARCHES",
    "MAX_WIDENING_ROUNDS",
    "TRAVERSAL_ORIGIN_EXPAND",
    "TRAVERSAL_ORIGIN_SEARCH",
    "TRAVERSAL_ORIGIN_SLICE",
    "TRAVERSAL_SYSTEM_PROMPT",
    "TraversalAction",
    "TraversalDecision",
    "build_pool_previews",
    "build_traversal_prompt",
    "neighbors_to_candidates",
    "parse_traversal_decision",
    "run_agentic_traversal",
    "slice_to_candidate",
]
