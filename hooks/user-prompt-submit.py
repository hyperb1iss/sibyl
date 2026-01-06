#!/usr/bin/env python3
"""Sibyl UserPromptSubmit Hook - Intelligent context injection.

Uses Haiku 4.5 to generate semantic search queries from conversation
context, then injects relevant knowledge from Sibyl's graph.

Architecture:
  Hook Input → Transcript Parse → Haiku 4.5 (query gen) → Sibyl Search → Format → Output

Latency budget: <500ms total
  - Transcript parse: <50ms
  - Haiku 4.5: <250ms
  - Sibyl search: <150ms
  - Format: <50ms
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Minimum prompt length to trigger search
MIN_PROMPT_LENGTH = 20

# Maximum transcript messages to consider
MAX_TRANSCRIPT_MESSAGES = 10

# Haiku model for query generation
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Query generation prompt - kept minimal for speed
QUERY_PROMPT = """Based on this conversation context, generate a concise search query (3-7 words) to find relevant patterns, learnings, or documentation from a knowledge graph.

Focus on: technical concepts, API names, frameworks, patterns being discussed.
Skip: generic terms, obvious things, conversation filler.

If the prompt is a command (starts with /), short greeting, or doesn't need external knowledge, respond with just: SKIP

Context:
{context}

Current prompt: {prompt}

Search query (or SKIP):"""


def parse_transcript(transcript_path: str) -> dict[str, Any]:
    """Parse transcript JSONL to extract working context.

    Returns:
        {
            "recent_messages": list of recent user/assistant messages,
            "files_edited": list of recently edited file paths,
            "tools_used": list of recent tool names,
            "focus_summary": brief summary of what's being worked on
        }
    """
    context: dict[str, Any] = {
        "recent_messages": [],
        "files_edited": set(),
        "tools_used": set(),
    }

    try:
        path = Path(transcript_path)
        if not path.exists():
            return context

        # Read JSONL (each line is a JSON object)
        lines = path.read_text().strip().split("\n")

        # Process recent entries (last N)
        for line in lines[-50:]:  # Look at last 50 entries for context
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type", "")

            # Extract messages
            if entry_type == "message":
                role = entry.get("role", "")
                content = entry.get("content", "")

                # Skip empty or system messages
                if not content or role == "system":
                    continue

                # Handle content that's a list (tool results, etc)
                if isinstance(content, list):
                    text_parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                    content = " ".join(text_parts)

                if content and len(content) > 10:
                    context["recent_messages"].append(
                        {
                            "role": role,
                            "content": content[:500],  # Truncate long messages
                        }
                    )

            # Extract tool usage
            elif entry_type == "tool_use":
                tool_name = entry.get("name", "")
                if tool_name:
                    context["tools_used"].add(tool_name)

                # Track file edits
                tool_input = entry.get("input", {})
                if tool_name in ("Edit", "Write", "Read") and "file_path" in tool_input:
                    context["files_edited"].add(tool_input["file_path"])

    except Exception:
        pass  # Graceful degradation

    # Convert sets to lists and limit
    context["files_edited"] = list(context["files_edited"])[-5:]
    context["tools_used"] = list(context["tools_used"])[-10:]
    context["recent_messages"] = context["recent_messages"][-MAX_TRANSCRIPT_MESSAGES:]

    return context


def build_context_string(transcript_context: dict[str, Any], prompt: str) -> str:
    """Build a concise context string for Haiku."""
    parts = []

    # Add recent conversation (abbreviated)
    if transcript_context.get("recent_messages"):
        msg_parts = []
        for msg in transcript_context["recent_messages"][-5:]:
            role = msg["role"]
            content = msg["content"][:150]
            msg_parts.append(f"{role}: {content}")
        if msg_parts:
            parts.append("Recent conversation:\n" + "\n".join(msg_parts))

    # Add files being worked on
    if transcript_context.get("files_edited"):
        files = transcript_context["files_edited"]
        parts.append(f"Files: {', '.join(files)}")

    return "\n\n".join(parts)


def generate_query_with_haiku(context: str, prompt: str) -> str | None:
    """Use Haiku 4.5 to generate a semantic search query.

    Uses raw HTTP to avoid SDK dependency.

    Returns:
        Search query string, or None if should skip
    """
    import urllib.error
    import urllib.request

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        payload = json.dumps(
            {
                "model": HAIKU_MODEL,
                "max_tokens": 50,
                "messages": [
                    {"role": "user", "content": QUERY_PROMPT.format(context=context, prompt=prompt)}
                ],
            }
        ).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )

        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode())

        # Extract text response
        content = data.get("content", [])
        if content and len(content) > 0:
            query = content[0].get("text", "").strip()

            # Check for skip signal (Haiku sometimes adds explanations after SKIP)
            if query.upper().startswith("SKIP") or len(query) < 5:
                if os.environ.get("SIBYL_HOOK_DEBUG"):
                    # Log skip reason on one line (replace newlines)
                    reason = query.replace("\n", " ").strip()[:100]
                    print(f"[sibyl-hook] SKIP: {reason}", file=sys.stderr)
                return None

            # Clean up query (remove quotes, etc)
            query = query.strip("\"'")
            return query

    except (urllib.error.URLError, json.JSONDecodeError, KeyError):
        pass

    return None


def run_sibyl(*args: str, timeout: int = 4) -> str | None:
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


def fallback_extract_terms(prompt: str) -> str | None:
    """Fallback: extract search terms without LLM (original logic)."""
    import re

    stop_words = {
        "about",
        "actually",
        "after",
        "again",
        "also",
        "been",
        "before",
        "between",
        "class",
        "code",
        "continue",
        "could",
        "during",
        "file",
        "from",
        "function",
        "further",
        "going",
        "have",
        "help",
        "here",
        "into",
        "just",
        "keep",
        "know",
        "like",
        "make",
        "method",
        "more",
        "need",
        "once",
        "only",
        "other",
        "please",
        "really",
        "should",
        "some",
        "thanks",
        "that",
        "then",
        "there",
        "think",
        "this",
        "through",
        "very",
        "want",
        "what",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
        "without",
        "would",
    }

    words = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", prompt.lower())
    terms = [w for w in words if len(w) > 3 and w not in stop_words and not w.startswith("_")]

    seen = set()
    unique = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    query = " ".join(unique[:5])
    return query if len(query) >= 8 else None


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
    start_time = time.time()

    try:
        data = json.load(sys.stdin)
        prompt = data.get("prompt", "")
        transcript_path = data.get("transcript_path", "")

        # Skip short prompts
        if len(prompt) < MIN_PROMPT_LENGTH:
            sys.exit(0)

        # Skip commands
        if prompt.strip().startswith("/"):
            sys.exit(0)

        # Parse transcript for context (fast: <50ms)
        transcript_context = {}
        if transcript_path:
            transcript_context = parse_transcript(transcript_path)

        # Build context string
        context_str = build_context_string(transcript_context, prompt)

        # Generate query with Haiku (target: <250ms)
        search_query = None
        if context_str or len(prompt) > 30:
            search_query = generate_query_with_haiku(context_str, prompt)

        # Fallback to stopword extraction if Haiku unavailable/fails
        if not search_query:
            search_query = fallback_extract_terms(prompt)

        if not search_query:
            sys.exit(0)

        # Search Sibyl (target: <150ms)
        output = run_sibyl("search", search_query, "--limit", "3", "-j", timeout=3)
        if not output:
            sys.exit(0)

        # Parse results
        try:
            search_data = json.loads(output)
            results = search_data.get("results", [])
        except json.JSONDecodeError:
            sys.exit(0)

        if not results:
            sys.exit(0)

        # Format for injection
        formatted = format_results(results)
        if not formatted:
            sys.exit(0)

        # Check total time (warn if over budget in debug mode)
        elapsed = time.time() - start_time
        debug_info = ""
        if os.environ.get("SIBYL_HOOK_DEBUG"):
            debug_info = f"\n_Query: `{search_query}` ({elapsed:.2f}s)_"

        # Output as additional context
        response = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": f"**Sibyl Context:**\n{formatted}{debug_info}",
            }
        }
        print(json.dumps(response))
        sys.exit(0)

    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
