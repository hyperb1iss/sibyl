"""Eval: does the agent invoke the `sibyl` skill when it should?

Spawns a Claude Agent SDK session per prompt, watches the tool-use stream,
and classifies the outcome as:

    SKILL_LOAD    - explicit Skill(sibyl) tool call OR `sibyl skill get`
    SIBYL_USE     - other `sibyl <subcommand>` Bash call (recall, search, etc.)
                    without an explicit skill load
    NONE          - no sibyl-related tool use at all

For trigger prompts we want SKILL_LOAD (best) or SIBYL_USE (acceptable).
For no-trigger prompts we want NONE.

Usage:
    uv run --with claude-agent-sdk python -m tools.eval.skill_invocation
    uv run --with claude-agent-sdk python -m tools.eval.skill_invocation --json
    uv run --with claude-agent-sdk python -m tools.eval.skill_invocation \\
        --model claude-sonnet-4-6 --runs 2

Requires ANTHROPIC_API_KEY in env, and a Sibyl install reachable via `sibyl`
in PATH (the agent will actually shell out, so the local stack must be up
if you want real recall data — but the eval scores invocation, not results).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from tools.eval.prompts import PROMPTS, EvalPrompt

Outcome = Literal["SKILL_LOAD", "SIBYL_USE", "NONE"]
DEFAULT_MODEL = "claude-opus-4-7"
# `sibyl skill get` and `sibyl skill list` both qualify as explicit skill loads.
SKILL_SUBCOMMAND_MIN_TOKENS = 3


@dataclass
class PromptResult:
    prompt: EvalPrompt
    outcome: Outcome
    tool_uses: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def pass_strict(self) -> bool:
        if self.prompt.expected == "trigger":
            return self.outcome == "SKILL_LOAD"
        return self.outcome == "NONE"

    @property
    def pass_lenient(self) -> bool:
        if self.prompt.expected == "trigger":
            return self.outcome in ("SKILL_LOAD", "SIBYL_USE")
        return self.outcome == "NONE"


def classify_tool_use(tool_uses: list[dict[str, Any]]) -> tuple[Outcome, list[str]]:
    """Walk tool-use blocks and decide what kind of sibyl engagement happened."""
    skill_load = False
    sibyl_use = False
    summaries: list[str] = []

    for use in tool_uses:
        name = use.get("name", "")
        inp = use.get("input") or {}
        if name == "Skill":
            target = str(inp.get("skill", ""))
            summaries.append(f"Skill({target})")
            if target == "sibyl":
                skill_load = True
        elif name == "Bash":
            command = str(inp.get("command", ""))
            stripped = command.lstrip()
            if not stripped.startswith("sibyl"):
                continue
            summaries.append(f"Bash({stripped[:80]})")
            head = stripped.split()
            if (
                len(head) >= SKILL_SUBCOMMAND_MIN_TOKENS
                and head[0] == "sibyl"
                and head[1] == "skill"
            ):
                skill_load = True
            else:
                sibyl_use = True

    if skill_load:
        return "SKILL_LOAD", summaries
    if sibyl_use:
        return "SIBYL_USE", summaries
    return "NONE", summaries


async def run_one(
    prompt: EvalPrompt,
    *,
    model: str,
    max_turns: int,
    timeout: float,
) -> PromptResult:
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
            outcome="NONE",
            error=f"claude-agent-sdk not installed: {exc}. "
            f"Re-run with `uv run --with claude-agent-sdk ...`.",
        )

    options = ClaudeAgentOptions(
        model=model,
        max_turns=max_turns,
        setting_sources=["user", "project"],
        # We don't want the agent to actually mutate state during the eval.
        # Skill load + read-only Bash is enough signal.
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
        return PromptResult(
            prompt=prompt,
            outcome="NONE",
            tool_uses=[u.get("name", "?") for u in tool_uses],
            error=f"timed out after {timeout:.0f}s",
        )
    except Exception as exc:
        return PromptResult(
            prompt=prompt,
            outcome="NONE",
            tool_uses=[u.get("name", "?") for u in tool_uses],
            error=f"{type(exc).__name__}: {exc}",
        )

    outcome, summaries = classify_tool_use(tool_uses)
    return PromptResult(prompt=prompt, outcome=outcome, tool_uses=summaries)


def render_table(results: list[PromptResult]) -> str:
    rows = []
    header = f"{'expected':<10}  {'outcome':<11}  {'pass':<6}  prompt"
    rows.append(header)
    rows.append("-" * len(header))
    for r in results:
        verdict = "STRICT" if r.pass_strict else ("LENIENT" if r.pass_lenient else "FAIL")
        preview = r.prompt.prompt.replace("\n", " ")[:60]
        rows.append(f"{r.prompt.expected:<10}  {r.outcome:<11}  {verdict:<6}  {preview}")
        if r.error:
            rows.append(f"    error: {r.error}")
        if r.tool_uses:
            rows.append(f"    tools: {', '.join(r.tool_uses[:4])}")
    return "\n".join(rows)


def summarize(results: list[PromptResult]) -> dict[str, Any]:
    by_outcome = Counter(r.outcome for r in results)
    triggers = [r for r in results if r.prompt.expected == "trigger"]
    no_triggers = [r for r in results if r.prompt.expected == "no-trigger"]
    return {
        "total": len(results),
        "outcomes": dict(by_outcome),
        "trigger_strict_pass_rate": _rate(triggers, lambda r: r.pass_strict),
        "trigger_lenient_pass_rate": _rate(triggers, lambda r: r.pass_lenient),
        "no_trigger_pass_rate": _rate(no_triggers, lambda r: r.pass_strict),
    }


def _rate(items: list[PromptResult], pred) -> float:
    if not items:
        return 0.0
    return round(sum(1 for i in items if pred(i)) / len(items), 3)


async def main_async(args: argparse.Namespace) -> int:
    if shutil.which("sibyl") is None:
        print("WARN: `sibyl` not in PATH; agents will fail to invoke the CLI.", file=sys.stderr)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2

    prompts = list(PROMPTS) * args.runs
    results: list[PromptResult] = []
    for p in prompts:
        result = await run_one(p, model=args.model, max_turns=args.max_turns, timeout=args.timeout)
        results.append(result)
        if not args.json:
            verdict = (
                "STRICT" if result.pass_strict else ("LENIENT" if result.pass_lenient else "FAIL")
            )
            print(f"  [{verdict:<7}] {result.outcome:<11} {p.prompt[:60]}", file=sys.stderr)

    summary = summarize(results)
    if args.json:
        payload = {
            "summary": summary,
            "results": [
                {
                    "prompt": r.prompt.prompt,
                    "expected": r.prompt.expected,
                    "outcome": r.outcome,
                    "pass_strict": r.pass_strict,
                    "pass_lenient": r.pass_lenient,
                    "tool_uses": r.tool_uses,
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
        print(
            f"trigger pass (strict):  {summary['trigger_strict_pass_rate']:.0%}\n"
            f"trigger pass (lenient): {summary['trigger_lenient_pass_rate']:.0%}\n"
            f"no-trigger pass:        {summary['no_trigger_pass_rate']:.0%}"
        )

    # Exit nonzero if either rate drops below the threshold.
    failed = (
        summary["trigger_lenient_pass_rate"] < args.threshold
        or summary["no_trigger_pass_rate"] < args.threshold
    )
    return 1 if failed else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"Anthropic model id (default: {DEFAULT_MODEL})"
    )
    parser.add_argument(
        "--runs", type=int, default=1, help="Repeat each prompt N times (default: 1)"
    )
    parser.add_argument("--max-turns", type=int, default=4, help="Agent SDK turn cap per prompt")
    parser.add_argument(
        "--timeout", type=float, default=90.0, help="Per-prompt wall-clock budget (seconds)"
    )
    parser.add_argument(
        "--threshold", type=float, default=0.7, help="Min pass rate to exit 0 (default: 0.7)"
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of table")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
