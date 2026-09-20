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
    """Quote whatever a handler did not name, one field per line, never as a blob.

    Scalars render as their value; anything structured is summarised by shape
    (a list by its length, a mapping by its keys) so an unhandled field can
    never carry raw host paths, argv or per-file digests into the pack.
    """
    lines = []
    for key in sorted(value):
        if key in consumed:
            continue
        item = value[key]
        if isinstance(item, list):
            lines.append(f"  {key}: list of {len(item)}")
        elif isinstance(item, dict):
            lines.append(f"  {key}: fields {', '.join(sorted(map(str, item))) or 'none'}")
        else:
            lines.extend([f"  {key}, quoted:", *quoted(item)])
    return lines


def _workspace(event: dict[str, Any]) -> list[str]:
    """One line for the workspace: unchanged, or the changed file names only."""
    before, after = event.get("workspace_before"), event.get("workspace_after")
    if before is None and after is None:
        return []
    if before == after:
        return ["  workspace unchanged"]
    if not isinstance(before, list) or not isinstance(after, list):
        return ["  workspace changed"]

    def entries(listing: list[Any]) -> dict[str, Any]:
        return {
            str(row.get("path")): row.get("sha256")
            for row in listing
            if isinstance(row, dict) and row.get("kind") != "directory"
        }

    old, new = entries(before), entries(after)
    changed = sorted(path for path in set(old) | set(new) if old.get(path) != new.get(path))
    return [f"  workspace changed: {len(changed)} files ({', '.join(changed)})"]


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


def _fields(label: str, value: Any) -> list[str]:
    """A labelled mapping rendered field by field through the same summariser."""
    if not isinstance(value, dict):
        return [f"{label}, quoted:", *quoted(value)]
    return [f"{label}:", *_rest(value)]


def _outcome(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return _fields("Recorded outcome", value)
    cases = value.get("cases")
    lines = _fields(
        "Recorded outcome", {key: item for key, item in value.items() if key != "cases"}
    )
    if isinstance(cases, list):
        passed = sum(
            1 for case in cases if isinstance(case, dict) and case.get("status") == "passed"
        )
        names = ", ".join(
            f"{case.get('id')} {case.get('status')}" for case in cases if isinstance(case, dict)
        )
        lines.append(f"  cases: {len(cases)} recorded, {passed} passed ({names})")
    return lines


def _request(event: dict[str, Any]) -> list[str]:
    lines = _fields("  controls", event.get("controls"))
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
    lines = [f"  status {event.get('status')}, exit code {event.get('returncode')}"]
    for key in ("refusal", "detail"):
        if event.get(key):
            lines.extend([f"  {key}, quoted:", *quoted(event[key])])
    for stream in ("stdout", "stderr"):
        if event.get(stream):
            lines.append(f"  recorded {stream}, quoted:")
            lines.extend(quoted(event[stream]))
    lines.extend(_workspace(event))
    return lines + _rest(
        event,
        "kind",
        "evidence_id",
        "status",
        "returncode",
        "refusal",
        "detail",
        "stdout",
        "stderr",
        "workspace_before",
        "workspace_after",
        "tool_call_id",
        "index",
        "cleanup",
        "carried_back",
        "timeout_seconds",
    )


def _tool_call(event: dict[str, Any]) -> list[str]:
    lines = [f"  the recorded agent called tool {event.get('name')!r}"]
    for key in ("command", "call"):
        if event.get(key) is not None:
            lines.extend([f"  {key}, quoted:", *quoted(event[key])])
    # argv is the sandbox launch line (docker, host bind mounts): never rendered.
    return lines + _rest(
        event, "kind", "evidence_id", "name", "command", "call", "argv", "tool_call_id", "index"
    )


def _start(event: dict[str, Any]) -> list[str]:
    workspace = event.get("workspace_initial")
    files = (
        sum(1 for row in workspace if isinstance(row, dict) and row.get("kind") != "directory")
        if isinstance(workspace, list)
        else None
    )
    return [
        "  that agent started in a sealed container"
        + (f" with {files} workspace files" if files is not None else "")
    ]


def _terminal(event: dict[str, Any]) -> list[str]:
    lines = [f"  that agent's run ended: reason {event.get('reason')}, exit {event.get('exit')}"]
    if event.get("detail"):
        lines.extend(["  detail, quoted:", *quoted(event["detail"])])
    return lines


LABELS = {
    "start": "recorded start of that agent's run",
    "model_request": "recorded request that agent sent to its model",
    "model_response": "recorded reply that agent's model returned",
    "tool_call": "recorded tool call that agent made",
    "tool_result": "recorded tool result that agent received",
    "terminal": "recorded end of that agent's run",
}


HANDLERS = {
    "model_request": _request,
    "model_response": _response,
    "tool_result": _tool_result,
    "tool_call": _tool_call,
    "start": _start,
    "terminal": _terminal,
}


def render_event(event: dict[str, Any]) -> list[str]:
    kind = event.get("kind")
    label = LABELS.get(kind, f"recorded {kind} event")
    handler = HANDLERS.get(kind, lambda value: _rest(value, "kind", "evidence_id"))
    return [f"[{event.get('evidence_id')} {label}]", *handler(event)]


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
        *_fields("Assignment record", view.get("assignment")),
        *_outcome(view.get("outcome")),
        *_fields("Evidence ids", view.get("evidence_ids")),
        *_rest(view, "episode_id", "goal", "assignment", "outcome", "evidence_ids", "events"),
        "Recorded events:",
    ]
    for event in view.get("events") or ():
        lines.extend(render_event(event))
    lines.extend([CLOSING, "</historical-episode>"])
    return "\n".join(lines) + "\n"
