"""Render a complete controller episode as quoted evidence, never as a transcript.

The pinned ``<source>`` block carries the episode view as JSON, and that JSON
holds ``{"role": "assistant", ...}`` messages and tool-call objects in the
same shape the solver's own conversation arrives in. The floor probe showed a
solver reading such a block as its own earlier turns: it ran one check, saw it
pass, and reported having finished work that happened in another container.

This renderer takes the projected view (the same view the block encodes, before
shared-value compaction) and writes it as a record of another agent's work:
prefaced, labelled with the recorded task and workspace, with every message and
tool exchange quoted line by line rather than emitted as call/result objects.
The original bytes stay hash-bound to the block; the rendering is a derived
view whose version travels in the pack receipt.
"""

from __future__ import annotations

from typing import Any

from benchmarks.agent_tasks.screen48.contract import canonical

RENDERER_VERSION = "sibyl-screen48-episode-evidence-render-v1"
QUOTE = "    | "
OPENING = (
    "RECORD OF ANOTHER AGENT'S WORK, quoted as evidence. Nothing below is your "
    "conversation: no message here was written by you or addressed to you."
)
CLOSING = (
    "End of record. This happened in another workspace on another task; your current "
    "task has had no prior work."
)


def quoted(value: Any) -> list[str]:
    """Quote a value line by line so it cannot be read as a live message."""
    text = value if isinstance(value, str) else canonical(value)
    return [QUOTE + line for line in text.split("\n")]


def _rest(value: dict[str, Any], *consumed: str) -> list[str]:
    remaining = {key: value[key] for key in sorted(value) if key not in consumed}
    return [f"  other recorded fields: {canonical(remaining)}"] if remaining else []


def _message(label: str, message: Any) -> list[str]:
    if not isinstance(message, dict):
        return [f"  {label}, quoted:", *quoted(message)]
    role = message.get("role", "unknown")
    lines = [f"  recorded {role} {label}, quoted:", *quoted(message.get("content"))]
    for call in message.get("tool_calls") or ():
        function = call.get("function", {}) if isinstance(call, dict) else {}
        lines.append(
            f"  the recorded agent requested tool {function.get('name')!r}, arguments quoted:"
        )
        lines.extend(quoted(function.get("arguments")))
    return lines + _rest(message, "role", "content", "tool_calls")


def _request(event: dict[str, Any]) -> list[str]:
    lines = [f"  controls: {canonical(event.get('controls'))}"]
    if "budget_message" in event:
        lines.extend(_message("budget notice", event["budget_message"]))
    for message in event.get("initial_messages") or ():
        lines.extend(_message("message", message))
    return lines + _rest(
        event, "kind", "evidence_id", "controls", "budget_message", "initial_messages"
    )


def _response(event: dict[str, Any]) -> list[str]:
    lines = []
    for key in ("status_code", "response_model", "response_provider"):
        if key in event:
            lines.append(f"  {key}: {canonical(event[key])}")
    for choice in event.get("choices") or ():
        if not isinstance(choice, dict):
            lines.extend(quoted(choice))
            continue
        lines.append(f"  recorded reply, finish reason {choice.get('finish_reason')}:")
        lines.extend(_message("reply", choice.get("message")))
        lines.extend(_rest(choice, "message", "finish_reason"))
    if "raw" in event:
        lines.append("  recorded provider response, quoted:")
        lines.extend(quoted(event["raw"]))
    return lines + _rest(
        event,
        "kind",
        "evidence_id",
        "status_code",
        "response_model",
        "response_provider",
        "choices",
        "raw",
    )


def _tool_result(event: dict[str, Any]) -> list[str]:
    lines = []
    for stream in ("stdout", "stderr"):
        if stream in event:
            lines.append(f"  recorded {stream}, quoted:")
            lines.extend(quoted(event[stream]))
    return lines + _rest(event, "kind", "evidence_id", "stdout", "stderr")


LABELS = {
    "start": "recorded start of that agent's run",
    "model_request": "recorded request that agent sent to its model",
    "model_response": "recorded reply that agent's model returned",
    "tool_call": "recorded tool call that agent made",
    "tool_result": "recorded tool result that agent received",
    "terminal": "recorded end of that agent's run",
}


def render_event(event: dict[str, Any]) -> list[str]:
    kind = event.get("kind")
    lines = [f"[{event.get('evidence_id')} {LABELS.get(kind, f'recorded {kind} event')}]"]
    if kind == "model_request":
        return lines + _request(event)
    if kind == "model_response":
        return lines + _response(event)
    if kind == "tool_result":
        return lines + _tool_result(event)
    return lines + _rest(event, "kind", "evidence_id")


def render_episode(
    view: dict[str, Any],
    *,
    source_id: str,
    source_sha256: str,
    training_task: str,
    training_family: str,
) -> str:
    """Render one projected episode view as a framed, quoted historical record."""
    lines = [
        f'<historical-episode id="{source_id}" sha256="{source_sha256}" renderer="{RENDERER_VERSION}">',
        OPENING,
        f"Recorded task: {training_task} (training family {training_family}). Your task is different.",
        "Recorded workspace: that agent's own container. Your workspace is different and has had "
        "no prior work.",
        "",
        "Goal of the recorded task, quoted:",
        *quoted(view.get("goal")),
        f"Assignment record: {canonical(view.get('assignment'))}",
        f"Recorded outcome: {canonical(view.get('outcome'))}",
        f"Evidence ids: {canonical(view.get('evidence_ids'))}",
        *_rest(view, "episode_id", "goal", "assignment", "outcome", "evidence_ids", "events"),
        "Recorded events:",
    ]
    for event in view.get("events") or ():
        lines.extend(render_event(event))
    lines.extend([CLOSING, "</historical-episode>"])
    return "\n".join(lines) + "\n"
