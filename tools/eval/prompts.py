"""Prompt set for the skill-invocation eval.

Each entry is (prompt, expected_outcome) where expected_outcome is:

    "trigger"     - skill load OR `sibyl skill get` is expected
    "no-trigger"  - prompt should NOT cause skill load (anti-trigger control)

Keep prompts realistic. The eval measures whether agents reach for the
skill on prompts that genuinely need persistent memory, and avoid it on
prompts that obviously don't.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ExpectedOutcome = Literal["trigger", "no-trigger"]


@dataclass(frozen=True)
class EvalPrompt:
    prompt: str
    expected: ExpectedOutcome
    why: str


PROMPTS: tuple[EvalPrompt, ...] = (
    EvalPrompt(
        prompt="what tasks am I currently working on in this project?",
        expected="trigger",
        why="Direct task-state question; needs `sibyl task list` and project context.",
    ),
    EvalPrompt(
        prompt="have we hit any issues with surrealdb hnsw rebuild before? "
        "i want to avoid repeating past mistakes.",
        expected="trigger",
        why="Past-incident lookup; needs `sibyl search` over error patterns.",
    ),
    EvalPrompt(
        prompt="please remember this for next time: the per-prompt context "
        "injection hook substituted for skill invocation and made agents lazy.",
        expected="trigger",
        why="Explicit capture request; should drive `sibyl remember` or `sibyl add`.",
    ),
    EvalPrompt(
        prompt="search the knowledge graph for embedding model evaluation patterns.",
        expected="trigger",
        why="Explicit graph search; obvious sibyl invocation.",
    ),
    EvalPrompt(
        prompt="i'm picking up where i left off. what's the current state of the longmemeval work?",
        expected="trigger",
        why="Session-resume question; needs `sibyl recall` and active task list.",
    ),
    EvalPrompt(
        prompt="we just solved a tricky bug with surreal lock contention by "
        "switching to driver.clone(group_id) per org. write up what we learned.",
        expected="trigger",
        why="Non-obvious learning; should be captured via `sibyl remember`.",
    ),
    EvalPrompt(
        prompt="what's 2 + 2?",
        expected="no-trigger",
        why="Trivial arithmetic; no persistent memory involved.",
    ),
    EvalPrompt(
        prompt="explain how python list comprehensions work, with one example.",
        expected="no-trigger",
        why="General programming knowledge; not project-specific.",
    ),
    EvalPrompt(
        prompt="rename the variable `foo` to `bar` in this snippet:\n"
        "```python\ndef double(foo):\n    return foo * 2\n```",
        expected="no-trigger",
        why="Self-contained refactor with all context in the prompt.",
    ),
)
