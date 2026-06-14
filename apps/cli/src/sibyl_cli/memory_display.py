"""Raw memory inspection helpers for CLI commands."""

from __future__ import annotations

from typing import Any, cast

from sibyl_cli.common import console, create_table
from sibyl_cli.id_resolution import resolve_raw_memory_id_prefix

RAW_MEMORY_REFERENCE_PREFIX = "raw_memory:"


def is_raw_memory_reference(value: str) -> bool:
    candidate = value.strip()
    return candidate.startswith(RAW_MEMORY_REFERENCE_PREFIX) or candidate.startswith("raw_memory_")


def raw_memory_lookup_value(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith(RAW_MEMORY_REFERENCE_PREFIX):
        return candidate.removeprefix(RAW_MEMORY_REFERENCE_PREFIX)
    return candidate


async def inspect_raw_memory_source(client: Any, value: str) -> dict[str, object]:
    resolved_source_id = await resolve_raw_memory_id_prefix(client, raw_memory_lookup_value(value))
    data = await client.memory_inspect(resolved_source_id)
    return cast("dict[str, object]", data)


def _format_memory_preview(content: str, max_chars: int = 220) -> str:
    preview = " ".join(content.strip().split())
    if len(preview) <= max_chars:
        return preview

    cutoff = preview.rfind(" ", 0, max_chars + 1)
    if cutoff < max_chars // 2:
        cutoff = max_chars
    return preview[:cutoff].rstrip() + "..."


def _format_policy_state(value: object) -> str:
    if value is True:
        return "allowed"
    if value is False:
        return "denied"
    return "n/a"


def _audit_id_summary(value: object, truncated: object = None) -> str:
    if not isinstance(value, list) or not value:
        return ""
    ids = [str(item) for item in value[:2]]
    stored_remainder = max(len(value) - 2, 0)
    hidden_count = (
        truncated if isinstance(truncated, int) and not isinstance(truncated, bool) else 0
    )
    remaining = stored_remainder + hidden_count
    if remaining:
        ids.append(f"+{remaining}")
    return ", ".join(ids)


def _inspect_correction_count(value: object) -> str:
    if isinstance(value, list):
        return str(len(value))
    return "0"


def _inspect_action_summary(value: object) -> str:
    if not isinstance(value, list):
        return "-"
    names: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        payload = cast("dict[str, object]", item)
        if payload.get("available") is True:
            names.append(str(payload.get("action")))
    return ", ".join(names) if names else "-"


def print_memory_source_inspect(data: dict[str, object], *, full_content: bool = False) -> None:
    console.print("\n[bold]Memory source[/bold]\n")
    scope = str(data.get("memory_scope") or "")
    if scope_key := data.get("scope_key"):
        scope = f"{scope}:{scope_key}" if scope else str(scope_key)
    policy = _format_policy_state(data.get("policy_allowed"))
    if reason := data.get("policy_reason"):
        policy = f"{policy} ({reason})"
    content_state = "redacted" if data.get("content_redacted") else "visible"

    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("ID", str(data.get("id") or ""))
    table.add_row("Source", str(data.get("source_id") or ""))
    table.add_row("Title", str(data.get("title") or ""))
    table.add_row("Scope", scope)
    table.add_row("Project", str(data.get("project_id") or ""))
    table.add_row("Review", str(data.get("review_state") or ""))
    promotion = data.get("promotion_state")
    if isinstance(promotion, dict):
        promotion_payload = cast("dict[str, object]", promotion)
        table.add_row("Promotion", str(promotion_payload.get("state") or ""))
    table.add_row("Corrections", _inspect_correction_count(data.get("correction_history")))
    table.add_row("Entity type", str(data.get("entity_type") or ""))
    table.add_row("Policy", policy)
    table.add_row("Content", content_state)
    table.add_row("Derived", _audit_id_summary(data.get("derived_ids")))
    table.add_row("Audits", str(data.get("audit_event_count") or 0))
    table.add_row("Actions", _inspect_action_summary(data.get("available_actions")))
    console.print(table)

    raw_content = data.get("raw_content")
    if isinstance(raw_content, str) and raw_content:
        console.print()
        content = raw_content if full_content else _format_memory_preview(raw_content)
        console.print(content, soft_wrap=True)
