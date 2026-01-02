"""Hook loading and merging for Claude Agent SDK.

Loads user hooks from Claude Code configuration and merges with Sibyl hooks.
Ensures Sibyl doesn't break user's existing Claude Code behavior.

Implements SDK-equivalent hooks for:
- UserPromptSubmit: Inject Sibyl context (same as ~/.claude/hooks/sibyl/user-prompt-submit.py)
- Stop: Detect workflow completion and flag for follow-up if needed
- PostToolUse: Track Sibyl MCP tool usage for workflow validation
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from claude_agent_sdk.types import (
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    SyncHookJSONOutput,
)

if TYPE_CHECKING:
    from sibyl_core.graph import EntityManager

log = structlog.get_logger()

# Claude Code config locations
USER_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
PROJECT_SETTINGS_FILE = ".claude/settings.json"
LOCAL_SETTINGS_FILE = ".claude/settings.local.json"

# Stop words for search term extraction (same as CLI hook)
STOP_WORDS = {
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

# Sibyl MCP tool patterns to track for workflow validation
SIBYL_TOOL_PATTERNS = [
    r"^mcp__sibyl",  # Any sibyl MCP tool
    r"sibyl_search",
    r"sibyl_add",
    r"sibyl_task",
    r"sibyl_entity",
]


@dataclass
class WorkflowTracker:
    """Tracks agent workflow compliance for Sibyl integration.

    Records what Sibyl-related actions the agent has taken to determine
    if the workflow was properly followed (search -> work -> capture).

    The workflow reminder is NOT rigid - it only triggers for substantive
    work sessions, not quick questions or simple tasks.
    """

    # Did agent search Sibyl for context?
    searched_sibyl: bool = False

    # Did agent interact with tasks?
    updated_task: bool = False

    # Did agent capture learnings?
    captured_learning: bool = False

    # Did agent receive Sibyl context injection?
    received_context: bool = False

    # The actual injected context (for UI display)
    injected_context: str | None = None

    # Raw tool calls for analysis
    sibyl_tool_calls: list[str] = field(default_factory=list)
    all_tool_calls: list[str] = field(default_factory=list)

    # Workflow state
    agent_stopped: bool = False

    # Thresholds for "substantive work" that warrants workflow reminder
    MIN_TOOL_CALLS_FOR_WORKFLOW = 5  # At least this many tool calls
    SUBSTANTIVE_TOOLS = {"Write", "Edit", "MultiEdit", "Bash"}  # Code-changing tools

    def record_tool_use(self, tool_name: str, tool_input: dict[str, Any] | None = None) -> None:
        """Record a tool use and update workflow state.

        Args:
            tool_name: Name of the tool called
            tool_input: Tool input parameters
        """
        self.all_tool_calls.append(tool_name)

        # Check if this is a Sibyl tool
        is_sibyl_tool = any(re.search(p, tool_name, re.IGNORECASE) for p in SIBYL_TOOL_PATTERNS)

        if is_sibyl_tool:
            self.sibyl_tool_calls.append(tool_name)

            # Categorize the action
            tool_lower = tool_name.lower()
            if "search" in tool_lower or "explore" in tool_lower:
                self.searched_sibyl = True
            elif "task" in tool_lower:
                self.updated_task = True
            elif "add" in tool_lower or "create" in tool_lower:
                # Check if it's adding knowledge vs creating a task
                if tool_input and "learning" in str(tool_input).lower():
                    self.captured_learning = True

    def _is_substantive_work(self) -> bool:
        """Check if this session involved substantive work worth tracking.

        Returns:
            True if agent did enough work to warrant a workflow reminder
        """
        # Not enough tool calls = quick task, skip reminder
        if len(self.all_tool_calls) < self.MIN_TOOL_CALLS_FOR_WORKFLOW:
            return False

        # Check if any substantive (code-changing) tools were used
        return any(
            any(sub in tool for sub in self.SUBSTANTIVE_TOOLS) for tool in self.all_tool_calls
        )

    def is_workflow_complete(self) -> bool:
        """Check if the Sibyl workflow was properly followed.

        Only triggers for substantive work sessions. Quick questions,
        simple lookups, or short tasks don't need the full workflow.

        Returns:
            True if workflow is complete OR task wasn't substantive enough
        """
        # If not substantive work, consider workflow "complete" (no reminder needed)
        if not self._is_substantive_work():
            return True

        # If agent received injected context, that counts as engaging with Sibyl
        has_context = self.searched_sibyl or self.received_context

        # For substantive work, require some Sibyl engagement
        return has_context or len(self.sibyl_tool_calls) > 0

    def should_remind(self) -> bool:
        """Check if we should send a workflow reminder.

        More explicit than is_workflow_complete() - use this to decide
        whether to send the follow-up message.

        Returns:
            True if a workflow reminder should be sent
        """
        return self._is_substantive_work() and not self.is_workflow_complete()

    def get_workflow_summary(self) -> dict[str, Any]:
        """Get a summary of workflow state for logging/debugging."""
        return {
            "searched_sibyl": self.searched_sibyl,
            "updated_task": self.updated_task,
            "captured_learning": self.captured_learning,
            "received_context": self.received_context,
            "sibyl_tool_calls": self.sibyl_tool_calls,
            "total_tool_calls": len(self.all_tool_calls),
            "is_substantive": self._is_substantive_work(),
            "agent_stopped": self.agent_stopped,
            "workflow_complete": self.is_workflow_complete(),
            "should_remind": self.should_remind(),
        }


def load_user_hooks(
    cwd: Path | str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Load hooks from user's Claude Code configuration.

    Checks (in order of precedence):
    1. ~/.claude/settings.json (global)
    2. {cwd}/.claude/settings.json (project)
    3. {cwd}/.claude/settings.local.json (local, gitignored)

    Args:
        cwd: Working directory for project/local settings

    Returns:
        Dict of hook event name -> list of hook configs (raw dicts, not HookMatchers)
    """
    hooks: dict[str, list[dict[str, Any]]] = {}

    # Load from each location, later ones override earlier
    paths_to_check = [USER_SETTINGS_PATH]

    if cwd:
        cwd_path = Path(cwd)
        paths_to_check.extend(
            [
                cwd_path / PROJECT_SETTINGS_FILE,
                cwd_path / LOCAL_SETTINGS_FILE,
            ]
        )

    for settings_path in paths_to_check:
        if not settings_path.exists():
            continue

        try:
            with open(settings_path) as f:
                settings = json.load(f)

            file_hooks = settings.get("hooks", {})
            if not isinstance(file_hooks, dict):
                continue

            # Merge hooks from this file
            for event, matchers in file_hooks.items():
                if not isinstance(matchers, list):
                    continue
                if event not in hooks:
                    hooks[event] = []
                hooks[event].extend(matchers)

            log.debug(f"Loaded hooks from {settings_path}: {list(file_hooks.keys())}")

        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Failed to load hooks from {settings_path}: {e}")

    return hooks


def merge_hooks(
    sibyl_hooks: dict[str, list[HookMatcher]] | None,
    user_hooks: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, list[HookMatcher]] | None:
    """Merge Sibyl hooks with user's Claude Code hooks.

    User hooks run FIRST (they configured them), Sibyl hooks run after.
    This preserves user expectations while adding Sibyl functionality.

    Note: User hooks from settings.json are raw dicts, not HookMatchers.
    The SDK handles converting them when passed to ClaudeAgentOptions.

    Args:
        sibyl_hooks: Sibyl's programmatic hooks (HookMatcher objects)
        user_hooks: User's hooks from settings.json (raw dicts)

    Returns:
        Merged hooks dict, or None if both inputs are None/empty
    """
    if not sibyl_hooks and not user_hooks:
        return None

    merged: dict[str, list[Any]] = {}

    # Add user hooks first (they run before ours)
    if user_hooks:
        for event, matchers in user_hooks.items():
            if event not in merged:
                merged[event] = []
            # User hooks are raw dicts - SDK will handle conversion
            merged[event].extend(matchers)

    # Add Sibyl hooks after (our hooks run after user's)
    if sibyl_hooks:
        for event, matchers in sibyl_hooks.items():
            if event not in merged:
                merged[event] = []
            merged[event].extend(matchers)

    return merged if merged else None  # type: ignore[return-value]


def _extract_search_terms(prompt: str) -> str:
    """Extract meaningful search terms from prompt.

    Same logic as ~/.claude/hooks/sibyl/user-prompt-submit.py
    """
    # Clean and tokenize
    words = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", prompt.lower())

    # Filter to meaningful terms
    terms = [w for w in words if len(w) > 3 and w not in STOP_WORDS and not w.startswith("_")]

    # Take unique terms, preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    # Return top terms
    return " ".join(unique[:5])


def _format_search_results(results: list[dict[str, Any]]) -> str:
    """Format search results for injection."""
    if not results:
        return ""

    lines: list[str] = []
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


class SibylContextService:
    """Provides Sibyl context injection for agent hooks.

    Replicates the behavior of ~/.claude/hooks/sibyl/user-prompt-submit.py
    but using direct graph access instead of subprocess.

    Also manages workflow tracking for Stop hook validation.
    """

    MIN_PROMPT_LENGTH = 25

    def __init__(
        self,
        entity_manager: "EntityManager",
        org_id: str,
        project_id: str | None = None,
    ):
        """Initialize context service.

        Args:
            entity_manager: Graph client for searching
            org_id: Organization UUID
            project_id: Optional project UUID for scoped searches
        """
        self.entity_manager = entity_manager
        self.org_id = org_id
        self.project_id = project_id
        self.workflow_tracker = WorkflowTracker()

    async def search_context(self, prompt: str) -> str | None:
        """Search Sibyl for context relevant to the prompt.

        Args:
            prompt: User's prompt text

        Returns:
            Formatted context string, or None if no relevant context found
        """
        # Skip short prompts
        if len(prompt) < self.MIN_PROMPT_LENGTH:
            return None

        # Skip commands
        if prompt.strip().startswith("/"):
            return None

        # Extract search terms
        search_terms = _extract_search_terms(prompt)
        if not search_terms or len(search_terms) < 8:
            return None

        try:
            # Search the graph
            results = await self.entity_manager.search(
                query=search_terms,
                limit=3,
            )

            if not results:
                return None

            # Convert Entity objects to dicts for formatting
            result_dicts = [
                {
                    "name": getattr(r, "name", getattr(r, "title", "")),
                    "entity_type": getattr(r, "entity_type", ""),
                    "summary": getattr(r, "summary", getattr(r, "content", "")),
                }
                for r in results
            ]

            formatted = _format_search_results(result_dicts)
            if not formatted:
                return None

            return f"**Sibyl Context:**\n{formatted}"

        except Exception as e:
            log.warning(f"Sibyl context search failed: {e}")
            return None

    def create_user_prompt_hook(self) -> HookMatcher:
        """Create UserPromptSubmit hook for context injection.

        Returns:
            HookMatcher for UserPromptSubmit events
        """

        async def inject_context(
            hook_input: HookInput,
            _tool_use_id: str | None,
            _context: HookContext,
        ) -> HookJSONOutput:
            """Hook callback to inject Sibyl context."""
            if hook_input.get("hook_event_name") != "UserPromptSubmit":
                return SyncHookJSONOutput(continue_=True)

            prompt = hook_input.get("prompt", "")
            additional_context = await self.search_context(prompt)

            if additional_context:
                log.debug(f"Injecting Sibyl context for prompt: {prompt[:50]}...")
                # Track that agent received context and store it for UI display
                self.workflow_tracker.received_context = True
                self.workflow_tracker.injected_context = additional_context
                return SyncHookJSONOutput(
                    continue_=True,
                    hookSpecificOutput={
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": additional_context,
                    },
                )

            return SyncHookJSONOutput(continue_=True)

        return HookMatcher(hooks=[inject_context])

    def create_post_tool_use_hook(self) -> HookMatcher:
        """Create PostToolUse hook to track Sibyl tool usage.

        Returns:
            HookMatcher for PostToolUse events
        """

        async def track_tool_use(
            hook_input: HookInput,
            _tool_use_id: str | None,
            _context: HookContext,
        ) -> HookJSONOutput:
            """Hook callback to track Sibyl tool usage."""
            if hook_input.get("hook_event_name") != "PostToolUse":
                return SyncHookJSONOutput(continue_=True)

            tool_name = hook_input.get("tool_name", "")
            tool_input = hook_input.get("tool_input", {})

            # Record the tool use in our tracker
            self.workflow_tracker.record_tool_use(
                tool_name,
                tool_input if isinstance(tool_input, dict) else None,
            )

            return SyncHookJSONOutput(continue_=True)

        return HookMatcher(hooks=[track_tool_use])


def create_stop_hook(workflow_tracker: WorkflowTracker) -> HookMatcher:
    """Create Stop hook for workflow completion detection.

    The Stop hook fires when the agent finishes execution.
    It marks the agent as stopped in the tracker so the caller
    can check workflow completion and send follow-up if needed.

    Args:
        workflow_tracker: Shared tracker to update on stop

    Returns:
        HookMatcher for Stop events
    """

    async def on_stop(
        hook_input: HookInput,
        _tool_use_id: str | None,
        _context: HookContext,
    ) -> HookJSONOutput:
        """Hook callback when agent stops."""
        if hook_input.get("hook_event_name") != "Stop":
            return SyncHookJSONOutput(continue_=True)

        # Mark agent as stopped
        workflow_tracker.agent_stopped = True

        # Log workflow summary
        summary = workflow_tracker.get_workflow_summary()
        log.info(f"Agent stopped. Workflow summary: {summary}")

        # Always continue - we handle follow-up logic externally
        return SyncHookJSONOutput(continue_=True)

    return HookMatcher(hooks=[on_stop])


def create_sibyl_hooks(
    approval_service: Any | None = None,
    context_service: SibylContextService | None = None,
) -> dict[str, list[HookMatcher]]:
    """Create Sibyl's hooks for context injection and workflow enforcement.

    Args:
        approval_service: ApprovalService for dangerous operation hooks
        context_service: SibylContextService for context injection

    Returns:
        Dict of hook event -> list of HookMatchers
    """
    hooks: dict[str, list[HookMatcher]] = {}

    # Add approval hooks if service provided
    if approval_service:
        approval_hooks = approval_service.create_hook_matchers()
        for event, matchers in approval_hooks.items():
            if event not in hooks:
                hooks[event] = []
            hooks[event].extend(matchers)

    # Add UserPromptSubmit hook for context injection
    if context_service:
        if "UserPromptSubmit" not in hooks:
            hooks["UserPromptSubmit"] = []
        hooks["UserPromptSubmit"].append(context_service.create_user_prompt_hook())

        # Add PostToolUse hook for tracking Sibyl tool usage
        if "PostToolUse" not in hooks:
            hooks["PostToolUse"] = []
        hooks["PostToolUse"].append(context_service.create_post_tool_use_hook())

        # Add Stop hook for workflow completion detection
        # The Stop hook captures when the agent finishes and flags for workflow validation
        if "Stop" not in hooks:
            hooks["Stop"] = []
        hooks["Stop"].append(create_stop_hook(context_service.workflow_tracker))

    return hooks
