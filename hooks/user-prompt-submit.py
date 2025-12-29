#!/usr/bin/env python3
"""Sibyl UserPromptSubmit Hook - Inject relevant knowledge.

Searches Sibyl for patterns and knowledge relevant to the user's
prompt and injects them as additional context.
"""

import json
import os
import re
import subprocess
import sys

# Minimum prompt length to trigger search
MIN_PROMPT_LENGTH = 25

# Words to skip when extracting search terms
STOP_WORDS = {
    "the",
    "this",
    "that",
    "with",
    "from",
    "have",
    "been",
    "will",
    "would",
    "could",
    "should",
    "about",
    "into",
    "what",
    "when",
    "where",
    "which",
    "while",
    "after",
    "before",
    "between",
    "through",
    "during",
    "without",
    "again",
    "further",
    "then",
    "once",
    "here",
    "there",
    "just",
    "only",
    "also",
    "very",
    "really",
    "actually",
    "please",
    "thanks",
    "help",
    "need",
    "want",
    "make",
    "some",
    "more",
    "other",
    "like",
    "know",
    "think",
    "code",
    "file",
    "function",
    "class",
    "method",
}


def run_sibyl(*args: str, timeout: int = 5) -> str | None:
    """Run sibyl command and return stdout."""
    try:
        result = subprocess.run(
            ["sibyl", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=os.environ.get("CLAUDE_PROJECT_DIR", "."),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def extract_search_terms(prompt: str) -> str:
    """Extract meaningful search terms from prompt."""
    # Clean and tokenize
    words = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", prompt.lower())

    # Filter to meaningful terms
    terms = [w for w in words if len(w) > 3 and w not in STOP_WORDS and not w.startswith("_")]

    # Take unique terms, preserving order
    seen = set()
    unique = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    # Return top terms
    return " ".join(unique[:5])


def format_results(results: list[dict]) -> str:
    """Format search results for injection."""
    if not results:
        return ""

    lines = []
    for r in results[:3]:
        name = r.get("name", r.get("title", ""))
        entity_type = r.get("entity_type", r.get("type", ""))
        summary = r.get("summary", r.get("content", ""))[:200]

        if name:
            type_label = f" ({entity_type})" if entity_type else ""
            lines.append(f"- **{name}**{type_label}")
            if summary:
                lines.append(f"  {summary}")

    return "\n".join(lines)


def main():
    try:
        data = json.load(sys.stdin)
        prompt = data.get("prompt", "")

        # Skip short prompts
        if len(prompt) < MIN_PROMPT_LENGTH:
            sys.exit(0)

        # Skip commands
        if prompt.strip().startswith("/"):
            sys.exit(0)

        # Extract search terms
        search_terms = extract_search_terms(prompt)
        if not search_terms or len(search_terms) < 8:
            sys.exit(0)

        # Search Sibyl (JSON output)
        output = run_sibyl("search", search_terms, "--limit", "3", "-j", timeout=4)
        if not output:
            sys.exit(0)

        # Parse results
        try:
            data = json.loads(output)
            results = data.get("results", [])
        except json.JSONDecodeError:
            sys.exit(0)

        if not results:
            sys.exit(0)

        # Format for injection
        formatted = format_results(results)
        if not formatted:
            sys.exit(0)

        # Output as additional context
        response = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": f"**Sibyl Context:**\n{formatted}",
            }
        }
        print(json.dumps(response))
        sys.exit(0)

    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
