"""Raw memory inspection helpers for CLI commands."""

from __future__ import annotations

from difflib import unified_diff
from itertools import pairwise
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
    table.add_row("Revision", str(data.get("revision") or ""))
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


def _dict_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [cast("dict[str, object]", item) for item in value if isinstance(item, dict)]


def _revision_number(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _print_blame_revisions(
    revisions: list[dict[str, object]],
    source: dict[str, object],
) -> None:
    current_content = source.get("raw_content")
    if not revisions and not isinstance(current_content, str):
        return
    console.print("\n[bold]Content revisions[/bold]\n")
    table = create_table(None, "Revision", "Changed", "Reason", expand=False)
    ordered = sorted(
        revisions,
        key=lambda item: _revision_number(item.get("revision")),
    )
    for item in ordered:
        table.add_row(
            str(item.get("revision") or ""),
            str(item.get("created_at") or ""),
            str(item.get("reason") or ""),
        )
    table.add_row(str(source.get("revision") or "current"), "current", "canonical body")
    console.print(table)

    versions = [
        (_revision_number(item.get("revision")), str(item.get("content") or "")) for item in ordered
    ]
    if isinstance(current_content, str):
        versions.append((_revision_number(source.get("revision")), current_content))
    for (prior_revision, prior), (next_revision, current) in pairwise(versions):
        lines = list(
            unified_diff(
                prior.splitlines(),
                current.splitlines(),
                fromfile=f"revision {prior_revision}",
                tofile=f"revision {next_revision}",
                lineterm="",
            )
        )
        if not lines:
            continue
        console.print(f"\n[dim]revision {prior_revision} → {next_revision}[/dim]")
        for line in lines[:200]:
            console.print(line, markup=False, soft_wrap=True)
        if len(lines) > 200:
            console.print(f"[dim]… {len(lines) - 200} diff lines omitted[/dim]")


def _print_blame_table(
    title: str,
    rows: list[dict[str, object]],
    columns: tuple[tuple[str, str], ...],
) -> None:
    if not rows:
        return
    console.print(f"\n[bold]{title}[/bold]\n")
    table = create_table(None, *(label for label, _ in columns), expand=False)
    for row in rows:
        table.add_row(*(str(row.get(key) or "") for _, key in columns))
    console.print(table)


def print_memory_source_blame(data: dict[str, object]) -> None:
    source_value = data.get("source")
    if not isinstance(source_value, dict):
        return
    source = cast("dict[str, object]", source_value)
    print_memory_source_inspect(source)
    _print_blame_revisions(_dict_items(data.get("content_revisions")), source)
    _print_blame_table(
        "Corrections",
        _dict_items(source.get("correction_history")),
        (("Action", "action"), ("Revision", "prior_revision"), ("Reason", "reason")),
    )
    _print_blame_table(
        "Import lineage",
        _dict_items(data.get("derived_from")),
        (("Import", "source_import_id"), ("Memory", "raw_memory_id"), ("Created", "created_at")),
    )
    _print_blame_table(
        "Supersessions",
        _dict_items(data.get("supersessions")),
        (
            ("Replacement", "raw_memory_id"),
            ("Superseded", "superseded_raw_memory_id"),
            ("Created", "created_at"),
        ),
    )
    _print_blame_table(
        "Derived records",
        _dict_items(source.get("derived_records")),
        (("ID", "id"), ("Type", "record_type"), ("Action", "source_action")),
    )
    _print_blame_table(
        "Audit trail",
        _dict_items(source.get("recent_audit_events")),
        (("Created", "created_at"), ("Action", "action"), ("Policy", "policy_reason")),
    )
