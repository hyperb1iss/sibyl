"""LongMemEval-V2 data loading helpers."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LongMemEvalV2Question:
    id: str
    domain: str
    environment: str
    question_type: str
    question: str
    image: str | None
    answer: str
    eval_function: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> LongMemEvalV2Question:
        return cls(
            id=_required_str(value, "id"),
            domain=_required_str(value, "domain"),
            environment=_required_str(value, "environment"),
            question_type=_required_str(value, "question_type"),
            question=_required_str(value, "question"),
            image=_optional_str(value.get("image")),
            answer=_required_str(value, "answer"),
            eval_function=_required_str(value, "eval_function"),
        )


@dataclass(frozen=True)
class LongMemEvalV2State:
    state_index: int
    step: int | None
    url: str
    action: str | None
    thought: str | None
    accessibility_tree: str
    screenshot: str | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> LongMemEvalV2State:
        return cls(
            state_index=_required_int(value, "state_index"),
            step=_optional_int(value.get("step")),
            url=_required_str(value, "url"),
            action=_optional_str(value.get("action")),
            thought=_optional_str(value.get("thought")),
            accessibility_tree=_required_str(value, "accessibility_tree"),
            screenshot=_optional_str(value.get("screenshot")),
        )


@dataclass(frozen=True)
class LongMemEvalV2Trajectory:
    id: str
    domain: str
    environment: str
    goal: str
    outcome: str
    start_url: str
    states: tuple[LongMemEvalV2State, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> LongMemEvalV2Trajectory:
        states = value.get("states")
        if not isinstance(states, list):
            msg = "LongMemEval-V2 trajectory states must be a list"
            raise ValueError(msg)
        return cls(
            id=_required_str(value, "id"),
            domain=_required_str(value, "domain"),
            environment=_required_str(value, "environment"),
            goal=_required_str(value, "goal"),
            outcome=_required_str(value, "outcome"),
            start_url=_required_str(value, "start_url"),
            states=tuple(LongMemEvalV2State.from_mapping(state) for state in states),
        )


def load_longmemeval_v2_questions(path: str | Path) -> list[LongMemEvalV2Question]:
    return [
        LongMemEvalV2Question.from_mapping(row)
        for row in _iter_jsonl_mappings(Path(path))
    ]


def load_longmemeval_v2_haystack(path: str | Path) -> dict[str, list[str]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = "LongMemEval-V2 haystack must be a JSON object"
        raise ValueError(msg)
    haystack: dict[str, list[str]] = {}
    for question_id, trajectory_ids in data.items():
        if not isinstance(question_id, str) or not isinstance(trajectory_ids, list):
            msg = "LongMemEval-V2 haystack maps question ids to trajectory id lists"
            raise ValueError(msg)
        haystack[question_id] = [str(trajectory_id) for trajectory_id in trajectory_ids]
    return haystack


def iter_longmemeval_v2_trajectories(
    path: str | Path,
) -> Iterable[LongMemEvalV2Trajectory]:
    for row in _iter_jsonl_mappings(Path(path)):
        yield LongMemEvalV2Trajectory.from_mapping(row)


def load_longmemeval_v2_trajectories(path: str | Path) -> dict[str, LongMemEvalV2Trajectory]:
    return {trajectory.id: trajectory for trajectory in iter_longmemeval_v2_trajectories(path)}


def select_longmemeval_v2_trajectories(
    path: str | Path,
    trajectory_ids: Iterable[str],
) -> dict[str, LongMemEvalV2Trajectory]:
    remaining = set(trajectory_ids)
    selected: dict[str, LongMemEvalV2Trajectory] = {}
    if not remaining:
        return selected
    for trajectory in iter_longmemeval_v2_trajectories(path):
        if trajectory.id not in remaining:
            continue
        selected[trajectory.id] = trajectory
        remaining.remove(trajectory.id)
        if not remaining:
            break
    return selected


def build_longmemeval_v2_trajectory_text(
    trajectory: LongMemEvalV2Trajectory,
    *,
    include_accessibility_tree: bool = True,
    include_screenshot_refs: bool = False,
    max_accessibility_chars: int | None = None,
) -> str:
    sections = [
        f"Trajectory: {trajectory.id}",
        f"Domain: {trajectory.domain}",
        f"Environment: {trajectory.environment}",
        f"Outcome: {trajectory.outcome}",
        f"Goal: {trajectory.goal}",
        f"Start URL: {trajectory.start_url}",
    ]
    for state in trajectory.states:
        state_parts = [
            f"State {state.state_index}",
            f"URL: {state.url}",
        ]
        if state.action:
            state_parts.append(f"Action: {state.action}")
        if state.thought:
            state_parts.append(f"Thought: {state.thought}")
        if include_screenshot_refs and state.screenshot:
            state_parts.append(f"Screenshot: {state.screenshot}")
        if include_accessibility_tree and state.accessibility_tree:
            tree = state.accessibility_tree
            if max_accessibility_chars is not None:
                tree = tree[:max_accessibility_chars]
            state_parts.append(f"Accessibility tree: {tree}")
        sections.append("\n".join(state_parts))
    return "\n\n".join(sections)


def summarize_longmemeval_v2_inputs(
    questions: Sequence[LongMemEvalV2Question],
    haystack: Mapping[str, Sequence[str]],
    *,
    trajectories: Mapping[str, LongMemEvalV2Trajectory] | None = None,
) -> dict[str, Any]:
    question_ids = {question.id for question in questions}
    haystack_question_ids = set(haystack)
    haystack_lengths = [len(value) for value in haystack.values()]
    payload: dict[str, Any] = {
        "question_count": len(questions),
        "haystack_count": len(haystack),
        "domain_counts": dict(Counter(question.domain for question in questions)),
        "question_type_counts": dict(Counter(question.question_type for question in questions)),
        "missing_haystack_questions": sorted(question_ids - haystack_question_ids),
        "orphan_haystack_questions": sorted(haystack_question_ids - question_ids),
        "haystack_min": min(haystack_lengths, default=0),
        "haystack_max": max(haystack_lengths, default=0),
    }
    if trajectories is not None:
        required = {trajectory_id for ids in haystack.values() for trajectory_id in ids}
        available = set(trajectories)
        payload.update(
            {
                "trajectory_count": len(trajectories),
                "missing_trajectory_count": len(required - available),
                "orphan_trajectory_count": len(available - required),
            }
        )
    return payload


def _iter_jsonl_mappings(path: Path) -> Iterable[Mapping[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                msg = f"Expected JSON object at {path}:{line_number}"
                raise ValueError(msg)
            yield row


def _required_str(value: Mapping[str, Any], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw:
        msg = f"LongMemEval-V2 field {key!r} must be a non-empty string"
        raise ValueError(msg)
    return raw


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        msg = "LongMemEval-V2 optional string field must be null or a string"
        raise ValueError(msg)
    return value


def _required_int(value: Mapping[str, Any], key: str) -> int:
    raw = value.get(key)
    if not isinstance(raw, int) or isinstance(raw, bool):
        msg = f"LongMemEval-V2 field {key!r} must be an integer"
        raise ValueError(msg)
    return raw


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        msg = "LongMemEval-V2 optional integer field must be null or an integer"
        raise ValueError(msg)
    return value
