"""Agent message formatting for UI display.

Formats Claude SDK messages into a structured format suitable for
WebSocket streaming and Postgres persistence.
"""

from datetime import UTC, datetime
from typing import Any


def get_tool_icon_and_preview(tool_name: str, tool_input: dict[str, Any]) -> tuple[str, str]:
    """Get icon name and preview text for a tool call."""
    if tool_name == "Read":
        path = tool_input.get("file_path", "file")
        # Show just filename or last 2 path segments
        short = "/".join(path.split("/")[-2:]) if "/" in path else path
        return "Page", f"`{short}`"
    if tool_name == "Write":
        path = tool_input.get("file_path", "file")
        short = "/".join(path.split("/")[-2:]) if "/" in path else path
        return "Page", f"Write `{short}`"
    if tool_name == "Edit":
        path = tool_input.get("file_path", "file")
        short = "/".join(path.split("/")[-2:]) if "/" in path else path
        return "EditPencil", f"`{short}`"
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        # Show the command directly, truncated
        return "Code", f"`{cmd[:60]}{'...' if len(cmd) > 60 else ''}`"
    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        path_hint = f" in {path.split('/')[-1]}" if path else ""
        return "Search", f"`{pattern[:50]}`{path_hint}"
    if tool_name == "Glob":
        return "Folder", f"`{tool_input.get('pattern', '')}`"
    if tool_name == "WebSearch":
        return "Globe", f"{tool_input.get('query', '')[:50]}"
    if tool_name == "WebFetch":
        url = tool_input.get("url", "")
        # Extract domain
        domain = url.split("//")[-1].split("/")[0] if "//" in url else url[:40]
        return "Globe", f"{domain}"
    if tool_name == "Task":
        return "User", f"{tool_input.get('description', '')[:50]}"
    if tool_name == "TodoWrite":
        return "List", "Updating todos"
    if tool_name == "LSP":
        op = tool_input.get("operation", "")
        return "Code", f"LSP {op}"
    return "Settings", tool_name


def generate_workflow_reminder(workflow_summary: dict[str, Any]) -> str:
    """Generate a follow-up prompt reminding about Sibyl workflow.

    Args:
        workflow_summary: Workflow state from WorkflowTracker

    Returns:
        Follow-up prompt for the agent
    """
    missing_steps: list[str] = []

    if not workflow_summary.get("searched_sibyl") and not workflow_summary.get("received_context"):
        missing_steps.append("search Sibyl for relevant patterns and past learnings")

    if not workflow_summary.get("updated_task"):
        missing_steps.append("update the task status if working on a tracked task")

    if not workflow_summary.get("captured_learning"):
        missing_steps.append("capture any non-obvious learnings discovered during this work")

    if not missing_steps:
        return "Please complete the Sibyl workflow before finishing."

    steps_text = "\n- ".join(missing_steps)
    return f"""Before finishing, please complete the Sibyl workflow:

- {steps_text}

This helps preserve learnings for future sessions. Use the Sibyl MCP tools (mcp__sibyl__search, mcp__sibyl__add, mcp__sibyl__manage) to complete these steps."""


def format_assistant_message(content: Any, timestamp: str) -> dict[str, Any]:
    """Format an AssistantMessage for UI display."""
    if isinstance(content, list):
        blocks = []
        for block in content:
            block_type = type(block).__name__
            if block_type == "TextBlock":
                blocks.append({"type": "text", "content": getattr(block, "text", "")})
            elif block_type == "ToolUseBlock":
                tool_name = getattr(block, "name", "unknown")
                tool_input = getattr(block, "input", {})
                tool_id = getattr(block, "id", "")
                if isinstance(tool_input, dict):
                    icon, preview = get_tool_icon_and_preview(tool_name, tool_input)
                    blocks.append(
                        {
                            "type": "tool_use",
                            "tool_name": tool_name,
                            "tool_id": tool_id,
                            "icon": icon,
                            "input": tool_input,
                            "preview": preview,
                        }
                    )

        if len(blocks) == 1:
            return {"role": "assistant", "timestamp": timestamp, **blocks[0]}
        return {
            "role": "assistant",
            "type": "multi_block",
            "blocks": blocks,
            "timestamp": timestamp,
            "preview": blocks[0].get("preview", "") if blocks else "",
        }
    return {
        "role": "assistant",
        "type": "text",
        "content": str(content) if content else "",
        "timestamp": timestamp,
        "preview": str(content)[:100] if content else "",
    }


def format_user_message(content: Any, timestamp: str) -> dict[str, Any]:
    """Format a UserMessage (usually tool results) for UI display."""
    if isinstance(content, list):
        results = []
        for block in content:
            if type(block).__name__ == "ToolResultBlock":
                tool_id = getattr(block, "tool_use_id", "")
                result_content = getattr(block, "content", "")
                is_error = getattr(block, "is_error", False)
                preview = str(result_content)[:200]
                if len(str(result_content)) > 200:
                    preview += "..."
                results.append(
                    {
                        "type": "tool_result",
                        "tool_id": tool_id,
                        "content": str(result_content),
                        "preview": preview,
                        "is_error": is_error,
                        "icon": "Xmark" if is_error else "Check",
                    }
                )
        if len(results) == 1:
            return {"role": "tool", "timestamp": timestamp, **results[0]}
        if results:
            return {
                "role": "tool",
                "type": "multi_result",
                "results": results,
                "timestamp": timestamp,
                "preview": f"{len(results)} tool results",
            }
    return {
        "role": "user",
        "type": "text",
        "content": str(content) if content else "",
        "timestamp": timestamp,
        "preview": str(content)[:100] if content else "",
    }


def format_agent_message(message: Any) -> dict[str, Any] | None:
    """Format a Claude SDK message for beautiful UI display.

    Returns None for internal SDK messages that shouldn't be shown to users.
    """
    msg_class = type(message).__name__
    content = getattr(message, "content", None)
    timestamp = datetime.now(UTC).isoformat()

    # Skip internal SDK messages that shouldn't be displayed
    # SystemMessage is Claude's internal system prompt - not for UI
    if msg_class in ("SystemMessage", "StopMessage"):
        return None

    # Extract parent_tool_use_id for subagent message grouping
    parent_tool_use_id = getattr(message, "parent_tool_use_id", None)

    if msg_class == "AssistantMessage":
        result = format_assistant_message(content, timestamp)
        if parent_tool_use_id:
            result["parent_tool_use_id"] = parent_tool_use_id
        return result

    if msg_class == "UserMessage":
        result = format_user_message(content, timestamp)
        # Skip user text messages - these are echoes of prompts we already stored
        # Only show tool results (role="tool") from UserMessage
        if result.get("role") == "user":
            return None
        if parent_tool_use_id:
            result["parent_tool_use_id"] = parent_tool_use_id
        return result

    if msg_class == "ResultMessage":
        usage = getattr(message, "usage", None)
        cost = getattr(message, "total_cost_usd", None)
        return {
            "role": "system",
            "type": "result",
            "icon": "Dollar",
            "session_id": getattr(message, "session_id", None),
            "usage": {
                "input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
                "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
            }
            if usage
            else None,
            "cost_usd": cost,
            "timestamp": timestamp,
            "preview": f"${cost:.4f}" if cost else "Completed",
        }

    return {
        "role": "unknown",
        "type": msg_class.lower(),
        "content": str(content) if content else "",
        "timestamp": timestamp,
        "preview": f"{msg_class}: {str(content)[:50]}" if content else msg_class,
    }
