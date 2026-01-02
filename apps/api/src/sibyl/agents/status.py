"""Status hint generation service using Haiku.

Generates clever, contextual waiting messages for agent tool calls.
Uses Claude Haiku for fast, cheap generation (~200ms, ~$0.001 per call).
"""

import hashlib
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

# Cache directory for generated hints (avoid repeated API calls)
CACHE_DIR = Path.home() / ".cache" / "sibyl" / "status_hints"

# Haiku model for fast generation
HAIKU_MODEL = "claude-3-5-haiku-latest"


class StatusHintService:
    """Generate contextual status hints using Haiku."""

    def __init__(self) -> None:
        self._client: Any = None
        self._cache: dict[str, str] = {}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def client(self) -> Any:
        """Lazy-load Anthropic client."""
        if self._client is None:
            try:
                import anthropic

                self._client = anthropic.Anthropic()
            except ImportError as e:
                msg = "anthropic package required"
                raise ImportError(msg) from e
        return self._client

    def _cache_key(self, tool_name: str, context: str) -> str:
        """Generate cache key for tool + context."""
        content = f"{tool_name}:{context[:200]}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _get_cached(self, tool_name: str, context: str) -> str | None:
        """Check cache for existing hint."""
        key = self._cache_key(tool_name, context)

        # Memory cache
        if key in self._cache:
            return self._cache[key]

        # Disk cache
        cache_file = CACHE_DIR / f"{key}.txt"
        if cache_file.exists():
            hint = cache_file.read_text().strip()
            self._cache[key] = hint
            return hint

        return None

    def _set_cached(self, tool_name: str, context: str, hint: str) -> None:
        """Cache hint to disk and memory."""
        key = self._cache_key(tool_name, context)
        self._cache[key] = hint
        cache_file = CACHE_DIR / f"{key}.txt"
        cache_file.write_text(hint)

    def generate_sync(
        self,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        task_title: str | None = None,
        agent_type: str | None = None,
    ) -> str:
        """Generate a contextual status hint synchronously.

        Args:
            tool_name: Name of the tool being called (Read, Edit, Grep, etc.)
            tool_input: Tool input parameters (file_path, pattern, etc.)
            task_title: Optional Sibyl task title for richer context
            agent_type: Optional agent type (general, frontend-developer, etc.)

        Returns:
            A clever, playful status phrase (3-8 words)
        """
        # Build context string for cache key
        context_parts = [tool_name]
        if tool_input:
            # Extract key identifiers
            if "file_path" in tool_input:
                context_parts.append(str(tool_input["file_path"]).split("/")[-1])
            if "pattern" in tool_input:
                context_parts.append(str(tool_input["pattern"])[:30])
            if "command" in tool_input:
                context_parts.append(str(tool_input["command"])[:30])
        if task_title:
            context_parts.append(task_title[:50])

        context = "|".join(context_parts)

        # Check cache first
        cached = self._get_cached(tool_name, context)
        if cached:
            log.debug(f"Status hint cache hit: {cached}")
            return cached

        # Build prompt
        prompt = self._build_prompt(tool_name, tool_input, task_title, agent_type)

        try:
            message = self.client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=30,
                messages=[{"role": "user", "content": prompt}],
            )
            hint = message.content[0].text.strip().strip('"').strip("'")

            # Validate length - should be short
            if len(hint) > 60:
                hint = hint[:57] + "..."

            self._set_cached(tool_name, context, hint)
            log.debug(f"Generated status hint: {hint}")
            return hint

        except Exception as e:
            log.warning(f"Failed to generate status hint: {e}")
            return self._fallback_hint(tool_name, tool_input)

    def _build_prompt(
        self,
        tool_name: str,
        tool_input: dict[str, Any] | None,
        task_title: str | None,
        agent_type: str | None,
    ) -> str:
        """Build the Haiku prompt."""
        # Extract relevant input details
        details = []
        if tool_input:
            if "file_path" in tool_input:
                details.append(f"File: {tool_input['file_path'].split('/')[-1]}")
            if "pattern" in tool_input:
                details.append(f"Pattern: {tool_input['pattern']}")
            if "command" in tool_input:
                cmd = str(tool_input["command"])[:50]
                details.append(f"Command: {cmd}")
            if "query" in tool_input:
                details.append(f"Query: {tool_input['query']}")

        context = ""
        if task_title:
            context += f"Working on: {task_title}\n"
        if agent_type:
            context += f"Agent type: {agent_type}\n"
        if details:
            context += "\n".join(details)

        return f"""Generate a short, clever status phrase (3-8 words) for an AI agent doing this:

Tool: {tool_name}
{context}

Style: Playful, slightly mystical, like "Consulting the code oracle" or "Weaving through the codebase".
Be creative but relevant to what's happening.

Reply with ONLY the phrase, no quotes or explanation."""

    def _fallback_hint(self, tool_name: str, tool_input: dict[str, Any] | None) -> str:
        """Fallback hints when Haiku fails."""
        fallbacks = {
            "Read": "Absorbing knowledge",
            "Edit": "Sculpting code",
            "Write": "Manifesting files",
            "Grep": "Hunting patterns",
            "Glob": "Mapping the terrain",
            "Bash": "Whispering to the shell",
            "Task": "Summoning allies",
            "WebSearch": "Consulting the web",
            "WebFetch": "Retrieving wisdom",
        }
        return fallbacks.get(tool_name, "Working magic")


# Global service instance
_service: StatusHintService | None = None


def get_status_service() -> StatusHintService:
    """Get or create the global status hint service."""
    global _service  # noqa: PLW0603
    if _service is None:
        _service = StatusHintService()
    return _service


def generate_status_hint(
    tool_name: str,
    tool_input: dict[str, Any] | None = None,
    task_title: str | None = None,
    agent_type: str | None = None,
) -> str:
    """Convenience function to generate a status hint.

    This is the main interface for the worker to call.
    """
    service = get_status_service()
    return service.generate_sync(tool_name, tool_input, task_title, agent_type)
