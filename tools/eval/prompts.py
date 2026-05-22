"""Prompt set for the skill-invocation eval.

Each prompt belongs to a category that probes a different kind of sibyl
engagement. The expected_verbs tuple lists the CLI verbs we'd accept as
correct for that prompt. An empty tuple marks an anti-trigger control
(prompt should NOT cause any sibyl invocation).

Verb names match the CLI surface. Multi-word verbs ("task list",
"entity show", "explore related") match the bash command head; see
the MULTI_WORD_VERBS table in skill_invocation.py for the full set.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvalPrompt:
    prompt: str
    category: str
    expected_verbs: tuple[str, ...]
    why: str
    max_turns: int = 4

    @property
    def is_trigger(self) -> bool:
        return bool(self.expected_verbs)


PROMPTS: tuple[EvalPrompt, ...] = (
    # ---------- session-resume ------------------------------------------
    EvalPrompt(
        prompt="where did i leave off?",
        category="session-resume",
        expected_verbs=("recall", "task list", "session bundle"),
        why="Generic resume question; recall, task list, or the session bundle all answer it.",
    ),
    EvalPrompt(
        prompt="i'm picking up the longmemeval work. what's the current state?",
        category="session-resume",
        expected_verbs=("recall", "task list", "search"),
        why="Topic-scoped resume; should pull working memory or active tasks for that topic.",
    ),
    # ---------- past-lookup ---------------------------------------------
    EvalPrompt(
        prompt="have we hit any issues with surrealdb hnsw rebuild before?",
        category="past-lookup",
        expected_verbs=("search", "recall"),
        why="Past-incident lookup; semantic search is the canonical move.",
    ),
    EvalPrompt(
        prompt="do we know anything about claude agent sdk hook loading behavior?",
        category="past-lookup",
        expected_verbs=("search", "recall"),
        why="Topic recall; check the graph before answering from training data.",
    ),
    # ---------- pattern-discovery ---------------------------------------
    EvalPrompt(
        prompt="what's our pattern for handling concurrent writes in sibyl?",
        category="pattern-discovery",
        expected_verbs=("search", "recall"),
        why="Pattern lookup; should search for stored patterns before improvising.",
    ),
    # ---------- capture-midwork -----------------------------------------
    EvalPrompt(
        prompt=(
            "remember this for next time: the per-prompt context injection hook "
            "substituted for skill invocation and made agents lazy."
        ),
        category="capture-midwork",
        expected_verbs=("remember", "add"),
        why="Explicit save request; remember (or its alias add) is correct.",
        max_turns=6,
    ),
    EvalPrompt(
        prompt=(
            "we just solved a tricky bug with surreal lock contention by switching "
            "to driver.clone(group_id) per org. write up what we learned."
        ),
        category="capture-midwork",
        expected_verbs=("remember", "add"),
        why="'Write up what we learned' is memory work, not a file Write.",
        max_turns=6,
    ),
    # ---------- capture-completion --------------------------------------
    EvalPrompt(
        prompt=(
            "i just finished implementing the auth gate. complete the task and "
            "capture this learning: invite tokens must be validated before user "
            "creation to avoid orphan rows."
        ),
        category="capture-completion",
        expected_verbs=("task complete", "remember"),
        why="Task completion with learnings; task complete --learnings is canonical.",
        max_turns=6,
    ),
    # ---------- task-lifecycle ------------------------------------------
    EvalPrompt(
        prompt="i'm starting work on the embedding model eval. mark it in progress.",
        category="task-lifecycle",
        expected_verbs=("task start", "task update", "task list"),
        why="Task state change; task start (or task update if id unknown, list first).",
    ),
    EvalPrompt(
        prompt=(
            "i'm blocked on the longmemeval task; need API keys from ops before i "
            "can proceed. mark it blocked."
        ),
        category="task-lifecycle",
        expected_verbs=("task block", "task update", "task list"),
        why="Task block; task block --reason or task update --status blocked.",
    ),
    # ---------- reflection ----------------------------------------------
    EvalPrompt(
        prompt=(
            "let's consolidate this session. we covered the hook yeet, built an "
            "eval, and tweaked the skill stub. save it for next time."
        ),
        category="reflection",
        expected_verbs=("reflect", "remember"),
        why="Session consolidation; reflect --persist is the canonical move.",
        max_turns=6,
    ),
    # ---------- graph-exploration ---------------------------------------
    EvalPrompt(
        prompt="what's connected to the longmemeval task? show me related work.",
        category="graph-exploration",
        expected_verbs=("explore related", "explore traverse", "entity related", "search"),
        why="Graph relationship query; explore subcommands or entity related.",
        max_turns=5,
    ),
    # ---------- pre-implementation --------------------------------------
    EvalPrompt(
        prompt=(
            "before i refactor the auth middleware, what gotchas have we seen "
            "with auth changes in this project?"
        ),
        category="pre-implementation",
        expected_verbs=("search", "recall"),
        why="Pre-implementation safety check; should search error_pattern entities.",
        max_turns=5,
    ),
    EvalPrompt(
        prompt=(
            "i'm about to add a new HNSW index. any error patterns related to "
            "surreal hnsw i should know about?"
        ),
        category="pre-implementation",
        expected_verbs=("search",),
        why="Explicit error_pattern lookup; search --type error_pattern works.",
        max_turns=5,
    ),
    # ---------- indirect-trigger ----------------------------------------
    EvalPrompt(
        prompt="i need to refactor the auth module. what should i know first?",
        category="indirect-trigger",
        expected_verbs=("search", "recall", "task list"),
        why="Could be answered from code reading alone, but graph lookup is the right first move.",
        max_turns=6,
    ),
    EvalPrompt(
        prompt="what's the deal with the longmemeval work? someone mentioned it.",
        category="indirect-trigger",
        expected_verbs=("search", "recall", "task list"),
        why="Ambient topic question; graph should be checked before guessing.",
        max_turns=6,
    ),
    # ---------- anti-trigger --------------------------------------------
    EvalPrompt(
        prompt="what's 2 + 2?",
        category="anti-trigger",
        expected_verbs=(),
        why="Trivial arithmetic; no persistent memory involved.",
    ),
    EvalPrompt(
        prompt="explain how python list comprehensions work, with one example.",
        category="anti-trigger",
        expected_verbs=(),
        why="General programming knowledge; not project-specific.",
    ),
    EvalPrompt(
        prompt=(
            "rename the variable `foo` to `bar` in this snippet:\n"
            "```python\ndef double(foo):\n    return foo * 2\n```"
        ),
        category="anti-trigger",
        expected_verbs=(),
        why="Self-contained refactor with all context in the prompt.",
    ),
    EvalPrompt(
        prompt="what does python's map() built-in do? short answer.",
        category="anti-trigger",
        expected_verbs=(),
        why="Stdlib documentation question; not project-specific.",
    ),
)


CATEGORIES: tuple[str, ...] = tuple(sorted({p.category for p in PROMPTS}))
