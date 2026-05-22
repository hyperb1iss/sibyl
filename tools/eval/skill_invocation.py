"""Eval: does the agent invoke the right `sibyl` verb when it should?

Spawns a Claude Agent SDK session per prompt, watches the tool-use stream,
extracts every `sibyl <verb>` it sees, and classifies the outcome against
the prompt's expected_verbs:

    CORRECT_VERB    - agent used a verb in expected_verbs (trigger only)
    WRONG_VERB      - agent used sibyl but the wrong verb (trigger only)
    MISS            - agent didn't use sibyl at all (trigger only)
    NO_VERB         - agent correctly avoided sibyl (anti-trigger only)
    FALSE_POSITIVE  - agent used sibyl on a non-trigger prompt

Strict pass: CORRECT_VERB on trigger, NO_VERB on anti-trigger.
Lenient pass: CORRECT_VERB or WRONG_VERB on trigger, NO_VERB on anti-trigger.

Usage:
    uv run --with claude-agent-sdk python -m tools.eval.skill_invocation
    uv run --with claude-agent-sdk python -m tools.eval.skill_invocation --json
    uv run --with claude-agent-sdk python -m tools.eval.skill_invocation \\
        --category capture-midwork --runs 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

from tools.eval.prompts import CATEGORIES, PROMPTS, EvalPrompt

Outcome = Literal[
    "CORRECT_VERB",
    "WRONG_VERB",
    "MISS",
    "NO_VERB",
    "FALSE_POSITIVE",
]
DEFAULT_MODEL = "claude-opus-4-7"

# Multi-token sibyl verbs that should be matched as a single unit when the
# expected_verbs entry has the same shape. Anything not in this set is
# treated as a single-token verb (e.g. `search`, `recall`, `remember`).
MULTI_WORD_VERBS: frozenset[str] = frozenset(
    {
        "task list",
        "task show",
        "task create",
        "task start",
        "task block",
        "task unblock",
        "task review",
        "task complete",
        "task archive",
        "task update",
        "task note",
        "task notes",
        "entity list",
        "entity show",
        "entity create",
        "entity related",
        "entity delete",
        "entity history",
        "explore related",
        "explore traverse",
        "explore dependencies",
        "project list",
        "project show",
        "project link",
        "project unlink",
        "project links",
        "project create",
        "epic list",
        "epic create",
        "epic show",
        "epic start",
        "epic complete",
        "epic archive",
        "context pack",
        "config show",
        "debug status",
        "debug schema",
        "debug query",
        "logs tail",
        "skill get",
        "skill list",
        "skill install",
        "crawl list",
        "crawl add",
        "crawl ingest",
        "crawl status",
        "crawl documents",
        "session bundle",
    }
)


@dataclass
class PromptResult:
    prompt: EvalPrompt
    outcome: Outcome
    verbs_used: list[str] = field(default_factory=list)
    tool_uses: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def pass_strict(self) -> bool:
        if self.prompt.is_trigger:
            return self.outcome == "CORRECT_VERB"
        return self.outcome == "NO_VERB"

    @property
    def pass_lenient(self) -> bool:
        if self.prompt.is_trigger:
            return self.outcome in ("CORRECT_VERB", "WRONG_VERB")
        return self.outcome == "NO_VERB"


def extract_sibyl_verb(command: str) -> str | None:
    """Return the sibyl verb invoked by a bash command, or None.

    Handles both single-word verbs (`sibyl search`) and the multi-word verbs
    listed in MULTI_WORD_VERBS (`sibyl task list`, `sibyl entity show`).
    """
    tokens = command.lstrip().split()
    if len(tokens) < 2 or tokens[0] != "sibyl":
        return None
    if len(tokens) >= 3:
        two_word = f"{tokens[1]} {tokens[2]}"
        if two_word in MULTI_WORD_VERBS:
            return two_word
    return tokens[1]


def classify(
    prompt: EvalPrompt, tool_uses: list[dict[str, Any]]
) -> tuple[Outcome, list[str], list[str]]:
    """Walk the tool-use stream and decide an outcome.

    Returns (outcome, verbs_used, tool_use_summaries).
    """
    verbs_used: list[str] = []
    summaries: list[str] = []

    for use in tool_uses:
        name = use.get("name", "")
        inp = use.get("input") or {}
        if name == "Skill":
            target = str(inp.get("skill", ""))
            summaries.append(f"Skill({target})")
            if target == "sibyl":
                # Skill load itself is not a verb invocation; the agent still
                # needs to run `sibyl skill get core` or another verb to
                # actually do the work. Recorded only in summaries.
                pass
        elif name == "Bash":
            command = str(inp.get("command", ""))
            stripped = command.lstrip()
            if not stripped.startswith("sibyl"):
                continue
            summaries.append(f"Bash({stripped[:80]})")
            verb = extract_sibyl_verb(stripped)
            if verb:
                verbs_used.append(verb)

    if prompt.is_trigger:
        if not verbs_used:
            return "MISS", verbs_used, summaries
        if any(v in prompt.expected_verbs for v in verbs_used):
            return "CORRECT_VERB", verbs_used, summaries
        return "WRONG_VERB", verbs_used, summaries
    # anti-trigger
    if not verbs_used:
        return "NO_VERB", verbs_used, summaries
    return "FALSE_POSITIVE", verbs_used, summaries


async def run_one(prompt: EvalPrompt, *, model: str, timeout: float) -> PromptResult:
    """Run a single agent session and capture tool-use intent."""
    try:
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            AssistantMessage,
            ClaudeAgentOptions,
            query,
        )
    except ImportError as exc:
        return PromptResult(
            prompt=prompt,
            outcome="MISS" if prompt.is_trigger else "NO_VERB",
            error=(
                f"claude-agent-sdk not installed: {exc}. "
                f"Re-run with `uv run --with claude-agent-sdk ...`."
            ),
        )

    options = ClaudeAgentOptions(
        model=model,
        max_turns=prompt.max_turns,
        setting_sources=["user", "project"],
        allowed_tools=["Skill", "Bash", "Read", "Glob", "Grep"],
    )

    tool_uses: list[dict[str, Any]] = []

    async def collect() -> None:
        async for msg in query(prompt=prompt.prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    block_dict = (
                        block if isinstance(block, dict) else getattr(block, "__dict__", {})
                    )
                    if block_dict.get("type") == "tool_use" or hasattr(block, "name"):
                        tool_uses.append(
                            {
                                "name": getattr(block, "name", block_dict.get("name", "")),
                                "input": getattr(block, "input", block_dict.get("input", {})),
                            }
                        )

    try:
        await asyncio.wait_for(collect(), timeout=timeout)
    except TimeoutError:
        outcome, verbs, summaries = classify(prompt, tool_uses)
        return PromptResult(
            prompt=prompt,
            outcome=outcome,
            verbs_used=verbs,
            tool_uses=summaries,
            error=f"timed out after {timeout:.0f}s",
        )
    except Exception as exc:
        outcome, verbs, summaries = classify(prompt, tool_uses)
        return PromptResult(
            prompt=prompt,
            outcome=outcome,
            verbs_used=verbs,
            tool_uses=summaries,
            error=f"{type(exc).__name__}: {exc}",
        )

    outcome, verbs, summaries = classify(prompt, tool_uses)
    return PromptResult(prompt=prompt, outcome=outcome, verbs_used=verbs, tool_uses=summaries)


def _rate(items: list[PromptResult], pred) -> float:
    if not items:
        return 0.0
    return round(sum(1 for i in items if pred(i)) / len(items), 3)


def per_category(results: list[PromptResult]) -> dict[str, dict[str, Any]]:
    """Group results by category and compute strict/lenient pass rates."""
    by_cat: dict[str, list[PromptResult]] = defaultdict(list)
    for r in results:
        by_cat[r.prompt.category].append(r)
    out: dict[str, dict[str, Any]] = {}
    for cat, rs in by_cat.items():
        out[cat] = {
            "count": len(rs),
            "strict": _rate(rs, lambda r: r.pass_strict),
            "lenient": _rate(rs, lambda r: r.pass_lenient),
            "outcomes": dict(Counter(r.outcome for r in rs)),
        }
    return out


def summarize(results: list[PromptResult]) -> dict[str, Any]:
    triggers = [r for r in results if r.prompt.is_trigger]
    anti = [r for r in results if not r.prompt.is_trigger]
    return {
        "total": len(results),
        "outcomes": dict(Counter(r.outcome for r in results)),
        "trigger_strict": _rate(triggers, lambda r: r.pass_strict),
        "trigger_lenient": _rate(triggers, lambda r: r.pass_lenient),
        "anti_trigger": _rate(anti, lambda r: r.pass_strict),
        "by_category": per_category(results),
    }


def render_table(results: list[PromptResult]) -> str:
    rows: list[str] = []
    header = f"{'category':<22}  {'outcome':<14}  {'pass':<7}  prompt"
    rows.append(header)
    rows.append("-" * len(header))
    for r in results:
        verdict = "STRICT" if r.pass_strict else ("LENIENT" if r.pass_lenient else "FAIL")
        preview = r.prompt.prompt.replace("\n", " ")[:50]
        rows.append(f"{r.prompt.category:<22}  {r.outcome:<14}  {verdict:<7}  {preview}")
        if r.verbs_used:
            rows.append(f"    verbs: {', '.join(r.verbs_used[:5])}")
        if r.error:
            rows.append(f"    error: {r.error}")
    return "\n".join(rows)


def render_category_summary(summary: dict[str, Any]) -> str:
    rows: list[str] = []
    header = f"{'category':<22}  {'count':>5}  {'strict':>7}  {'lenient':>8}"
    rows.append(header)
    rows.append("-" * len(header))
    for cat, stats in sorted(summary["by_category"].items()):
        rows.append(
            f"{cat:<22}  {stats['count']:>5}  {stats['strict']:>6.0%}  {stats['lenient']:>7.0%}"
        )
    return "\n".join(rows)


async def main_async(args: argparse.Namespace) -> int:
    if shutil.which("sibyl") is None:
        print("WARN: `sibyl` not in PATH; agents will fail to invoke the CLI.", file=sys.stderr)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2

    prompts: list[EvalPrompt] = list(PROMPTS)
    if args.category:
        prompts = [p for p in prompts if p.category == args.category]
        if not prompts:
            print(
                f"error: no prompts in category '{args.category}'. Known: {', '.join(CATEGORIES)}",
                file=sys.stderr,
            )
            return 2

    prompts = prompts * args.runs
    results: list[PromptResult] = []
    for p in prompts:
        result = await run_one(p, model=args.model, timeout=args.timeout)
        results.append(result)
        if not args.json:
            verdict = (
                "STRICT" if result.pass_strict else ("LENIENT" if result.pass_lenient else "FAIL")
            )
            print(
                f"  [{verdict:<7}] {result.outcome:<14} {p.category:<22} {p.prompt[:50]}",
                file=sys.stderr,
            )

    summary = summarize(results)
    if args.json:
        payload = {
            "summary": summary,
            "results": [
                {
                    "prompt": r.prompt.prompt,
                    "category": r.prompt.category,
                    "expected_verbs": list(r.prompt.expected_verbs),
                    "outcome": r.outcome,
                    "verbs_used": r.verbs_used,
                    "tool_uses": r.tool_uses,
                    "pass_strict": r.pass_strict,
                    "pass_lenient": r.pass_lenient,
                    "error": r.error,
                }
                for r in results
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        print()
        print(render_table(results))
        print()
        print(render_category_summary(summary))
        print()
        print(
            f"trigger strict:    {summary['trigger_strict']:.0%}\n"
            f"trigger lenient:   {summary['trigger_lenient']:.0%}\n"
            f"anti-trigger:      {summary['anti_trigger']:.0%}"
        )

    failed = summary["trigger_lenient"] < args.threshold or summary["anti_trigger"] < args.threshold
    return 1 if failed else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"Anthropic model id (default: {DEFAULT_MODEL})"
    )
    parser.add_argument(
        "--category",
        default=None,
        help=f"Run only prompts in this category. One of: {', '.join(CATEGORIES)}",
    )
    parser.add_argument("--runs", type=int, default=1, help="Repeat each prompt N times")
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="Per-prompt wall-clock budget (seconds)"
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.7,
        help="Min trigger-lenient + anti-trigger rate for exit 0 (default 0.7)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of table")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
