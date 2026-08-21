"""Shared presentation helpers for memory, synthesis, and administration commands."""

import re
from collections.abc import Mapping
from typing import Any, cast

from rich.markup import escape

from sibyl_cli.common import (
    CORAL,
    NEON_CYAN,
    console,
    create_table,
    error,
    info,
    print_json,
    print_mutation_receipt,
    success,
)

SEARCH_PREVIEW_CHARS = 360


def _format_search_preview(content: str, max_chars: int = SEARCH_PREVIEW_CHARS) -> str:
    """Format search result previews for terminal display."""
    preview = content.strip()
    if preview.startswith("[") and "] " in preview:
        preview = preview.split("] ", 1)[1]
    preview = " ".join(preview.split())
    if len(preview) <= max_chars:
        return preview

    cutoff = preview.rfind(" ", 0, max_chars + 1)
    if cutoff < max_chars // 2:
        cutoff = max_chars
    return preview[:cutoff].rstrip() + "…"


def format_highlight_preview(
    snippet: str | None,
    fallback: str,
    max_chars: int = SEARCH_PREVIEW_CHARS,
) -> str:
    raw = snippet or fallback
    preview = _format_search_preview(raw, max_chars=max_chars)
    if not snippet or ("<mark>" not in preview and "</mark>" not in preview):
        return escape(preview)

    parts = re.split(r"(<mark>|</mark>)", preview)
    active = False
    rendered: list[str] = []
    for part in parts:
        if part == "<mark>":
            active = True
            continue
        if part == "</mark>":
            active = False
            continue
        if not part:
            continue
        escaped = escape(part)
        if active:
            rendered.append(f"[bold {NEON_CYAN}]{escaped}[/]")
        else:
            rendered.append(escaped)
    return "".join(rendered)


def _print_probe_rehearsal(data: Mapping[str, Any]) -> None:
    """Show what each probe found, or that it found nothing."""
    receipt = data.get("probe_rehearsal")
    if not isinstance(receipt, Mapping):
        return
    entries = receipt.get("probes")
    if not isinstance(entries, list) or not entries:
        return
    total = receipt.get("total", len(entries))
    retrievable = receipt.get("retrievable", 0)
    console.print(f"  [dim]Probes: {retrievable}/{total} retrievable[/dim]")
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        status = str(entry.get("status", "unknown"))
        text = str(entry.get("probe", ""))
        if status == "retrievable":
            console.print(f"    [green]✓[/green] rank {entry.get('rank')} · {text}")
        elif status == "absent":
            console.print(f"    [yellow]✗ not retrievable[/yellow] · {text}")
        else:
            console.print(f"    [dim]{status}[/dim] · {text}")
    if receipt.get("truncated"):
        console.print("  [dim]Rehearsal stopped early on its time budget[/dim]")


def print_memory_capture_result(
    *,
    title: str,
    kind: str,
    data: dict[str, Any],
    wait_searchable: bool,
) -> None:
    entity_id = data.get("id", "unknown")
    if wait_searchable:
        success(f"Remembered {kind}: {title}")
    else:
        info(f"Queued {kind}: {title}")
    print_mutation_receipt(data)
    console.print(f"  [dim]ID: {entity_id}[/dim]")
    if raw_memory_id := data.get("raw_memory_id"):
        console.print(f"  [dim]Raw: {raw_memory_id}[/dim]")
    if raw_policy_reason := data.get("raw_policy_reason"):
        console.print(f"  [dim]Policy: {raw_policy_reason}[/dim]")
    _print_probe_rehearsal(data)


def print_reflection_persistence_summary(
    data: dict[str, object], *, persist: bool, persist_source: bool
) -> None:
    if not persist:
        return

    source_id = data.get("source_id")
    candidates = data.get("candidates")
    candidate_items = candidates if isinstance(candidates, list) else []
    persisted_ids: list[object] = []
    for item in candidate_items:
        if not isinstance(item, dict):
            continue
        candidate = cast("dict[str, object]", item)
        if persisted_id := candidate.get("persisted_id"):
            persisted_ids.append(persisted_id)
    persisted_count = data.get("persisted_count", len(persisted_ids))
    total_candidates = data.get("total_candidates", len(candidate_items))

    console.print()
    if persist_source:
        if source_id:
            success(f"Persisted source: {source_id}")
        else:
            info("Persisted source: unavailable")
    else:
        info("Source persistence skipped (--no-source)")

    success(f"Persisted candidates: {persisted_count}/{total_candidates}")
    for persisted_id in persisted_ids:
        console.print(f"  [dim]ID: {persisted_id}[/dim]")


def print_raw_memory_results(memories: list[object]) -> None:
    if not memories:
        info("No raw memories found")
        return

    console.print(f"\n[bold]Found {len(memories)} raw memories:[/bold]\n")
    for item in memories:
        if not isinstance(item, dict):
            continue
        memory = cast("dict[str, object]", item)
        title = str(memory.get("title") or "Untitled raw memory")
        source_id = str(memory.get("source_id") or "")
        memory_id = str(memory.get("id") or "")
        content = str(memory.get("raw_content") or "")
        snippet = str(memory.get("snippet") or "")
        score = memory.get("score")
        scope = str(memory.get("memory_scope") or "private")
        policy_reason = str(memory.get("policy_reason") or "")

        source_label = f" [dim]({source_id})[/dim]" if source_id else ""
        console.print(f"  [{NEON_CYAN}]{title}[/{NEON_CYAN}]{source_label}")
        if content or snippet:
            console.print(
                f"    {format_highlight_preview(snippet or None, content)}",
                soft_wrap=True,
            )
        score_label = f" score={score}" if score else ""
        policy_label = f" policy={policy_reason}" if policy_reason else ""
        console.print(f"    [dim]scope={scope}{score_label}{policy_label}[/dim]")
        console.print(f"    [{CORAL}]{memory_id}[/{CORAL}]")
        console.print()


def _int_value(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [cast("dict[str, object]", item) for item in value if isinstance(item, dict)]


def _source_pack_receipt_counts(data: dict[str, object]) -> tuple[int, int, int, int]:
    hidden = 0
    redacted = 0
    corrected = 0
    freshness = 0
    for pack in _dict_list(data.get("source_packs")):
        hidden += _int_value(pack.get("hidden_count"))
        redacted += _int_value(pack.get("redaction_count"))
        corrected += _int_value(pack.get("correction_count"))
        freshness_payload = pack.get("freshness")
        if isinstance(freshness_payload, dict):
            freshness += len(freshness_payload)
    return hidden, redacted, corrected, freshness


def _source_pack_correction_reasons(data: dict[str, object]) -> list[str]:
    reasons: list[str] = []
    seen: set[str] = set()
    for pack in _dict_list(data.get("source_packs")):
        for reason in _correction_reason_names(pack.get("correction_reasons")):
            if reason not in seen:
                reasons.append(reason)
                seen.add(reason)
    return reasons


def _correction_reason_names(value: object) -> list[str]:
    if isinstance(value, dict):
        return [str(reason) for reason in value]
    if isinstance(value, list):
        return [str(reason) for reason in value]
    return []


def _find_source_pack(data: dict[str, object], section_id: object) -> dict[str, object] | None:
    for pack in _dict_list(data.get("source_packs")):
        if pack.get("section_id") == section_id:
            return pack
    return None


def _print_source_pack_receipt(pack: dict[str, object]) -> None:
    source_ids = pack.get("source_ids")
    source_count = len(source_ids) if isinstance(source_ids, list) else 0
    hidden = _int_value(pack.get("hidden_count"))
    redacted = _int_value(pack.get("redaction_count"))
    corrected = _int_value(pack.get("correction_count"))
    if source_count:
        console.print(f"    [dim]sources: {source_count} receipt(s)[/dim]")
    if hidden or redacted or corrected:
        console.print(
            f"    [dim]impact: {hidden} hidden · {redacted} redacted · {corrected} corrected[/dim]"
        )
    reasons = _correction_reason_names(pack.get("correction_reasons"))
    if reasons:
        console.print(f"    [dim]corrections: {', '.join(reasons[:4])}[/dim]")


def print_synthesis_plan(data: dict[str, object]) -> None:
    outline = cast("dict[str, object]", data.get("outline") or {})
    title = str(outline.get("title") or "Synthesis Plan")
    sections = outline.get("sections")
    section_items = sections if isinstance(sections, list) else []
    verification = cast("dict[str, object]", data.get("verification") or {})
    console.print(f"\n[bold]{title}[/bold]")
    console.print(
        f"[dim]Run: {data.get('run_id')} · "
        f"verification={verification.get('status')} · "
        f"sources={verification.get('source_count', 0)}[/dim]\n"
    )
    for item in section_items:
        if not isinstance(item, dict):
            continue
        section = cast("dict[str, object]", item)
        source_ids = section.get("source_ids")
        source_count = len(source_ids) if isinstance(source_ids, list) else 0
        console.print(f"  [{NEON_CYAN}]{section.get('title')}[/{NEON_CYAN}]")
        console.print(f"    [dim]{source_count} source(s)[/dim]")
        gaps = section.get("gaps")
        for gap in gaps if isinstance(gaps, list) else []:
            if isinstance(gap, dict):
                gap_data = cast("dict[str, object]", gap)
                console.print(f"    [dim]gap: {gap_data.get('reason')}[/dim]")
        if pack := _find_source_pack(data, section.get("section_id")):
            _print_source_pack_receipt(pack)


def print_synthesis_verification(data: dict[str, object]) -> None:
    verification = cast("dict[str, object]", data.get("verification") or {})
    status = str(verification.get("status") or "unknown")
    source_count = verification.get("source_count", 0)
    gap_count = verification.get("gap_count", 0)
    if status == "pass":
        success(f"Synthesis verification passed ({source_count} sources)")
    else:
        error(f"Synthesis verification has gaps ({gap_count})")
    gaps = verification.get("gaps")
    for gap in gaps if isinstance(gaps, list) else []:
        if isinstance(gap, dict):
            gap_data = cast("dict[str, object]", gap)
            console.print(f"  [dim]{gap_data.get('title')}: {gap_data.get('reason')}[/dim]")
    hidden, redacted, corrected, freshness = _source_pack_receipt_counts(data)
    if hidden or redacted or corrected or freshness:
        console.print(
            f"  [dim]Correction impact: {hidden} hidden · {redacted} redacted · "
            f"{corrected} corrected · {freshness} freshness[/dim]"
        )
    reasons = _source_pack_correction_reasons(data)
    if reasons:
        console.print(f"  [dim]Correction reasons: {', '.join(reasons[:5])}[/dim]")


def print_synthesis_artifact(data: dict[str, object], *, output_format: str) -> None:
    artifact = cast("dict[str, object]", data.get("artifact") or {})
    if output_format == "json":
        print_json(cast("dict[str, object]", artifact.get("json_payload") or {}))
        return
    console.print(str(artifact.get("markdown") or ""))


def print_synthesis_remember(data: dict[str, object]) -> None:
    artifact = cast("dict[str, object]", data.get("artifact") or {})
    remembered_memory_id = artifact.get("remembered_memory_id")
    remembered_source_id = artifact.get("remembered_source_id")
    if remembered_memory_id:
        success(f"Remembered synthesis artifact: {artifact.get('title')}")
        console.print(f"  [dim]Artifact: {artifact.get('artifact_id', '')}[/dim]")
        console.print(f"  [dim]Memory: {remembered_memory_id}[/dim]")
        console.print(f"  [dim]Source: {remembered_source_id}[/dim]")
        source_ids = artifact.get("source_ids")
        if isinstance(source_ids, list) and source_ids:
            console.print(
                f"  [dim]Source receipts: {', '.join(str(item) for item in source_ids)}[/dim]"
            )
        return
    error("Synthesis artifact was drafted but not remembered.")


def _source_import_scope(data: dict[str, object]) -> str:
    scope = str(data.get("target_memory_scope") or "private")
    if scope_key := data.get("target_scope_key"):
        return f"{scope}:{scope_key}"
    return scope


def _source_import_progress(data: dict[str, object]) -> dict[str, object]:
    progress = data.get("progress")
    return cast("dict[str, object]", progress) if isinstance(progress, dict) else {}


def _source_import_safe_record_summary(record: dict[str, object]) -> str:
    for key in ("adapter_record_id", "source_uri", "code", "type"):
        if value := record.get(key):
            return str(value)
    return "record"


def print_source_import_status(data: dict[str, object]) -> None:
    progress = _source_import_progress(data)
    console.print("\n[bold]Source import receipt[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("Import Id", str(data.get("import_id") or ""))
    table.add_row("Status", str(data.get("status") or ""))
    table.add_row("Adapter", str(data.get("adapter_name") or ""))
    table.add_row("Source", str(data.get("source_identity") or ""))
    table.add_row("Target scope", _source_import_scope(data))
    table.add_row("Privacy", str(data.get("privacy_class") or "default"))
    table.add_row("Imported", str(progress.get("imported_count") or 0))
    table.add_row("Skipped", str(progress.get("skipped_count") or 0))
    table.add_row("Deduped", str(progress.get("dedupe_count") or 0))
    table.add_row("Errors", str(progress.get("error_count") or 0))
    table.add_row("Attachments", str(progress.get("attachment_count") or 0))
    table.add_row("Pending extraction", str(progress.get("extraction_pending_count") or 0))
    console.print(table)

    raw_memory_ids = data.get("raw_memory_ids")
    if isinstance(raw_memory_ids, list) and raw_memory_ids:
        console.print("\n[bold]Raw memory receipts[/bold]")
        for source_id in raw_memory_ids[:18]:
            console.print(f"  [{CORAL}]{source_id}[/{CORAL}]")

    skipped_records = _dict_list(data.get("skipped_records"))
    if skipped_records:
        console.print("\n[bold]Skipped records[/bold]")
        for record in skipped_records[:6]:
            reason = record.get("reason") or record.get("message") or "skipped"
            console.print(f"  [dim]{_source_import_safe_record_summary(record)}: {reason}[/dim]")

    errors = _dict_list(data.get("errors"))
    if errors:
        console.print("\n[bold]Errors[/bold]")
        for record in errors[:6]:
            message = record.get("message") or record.get("error") or "error"
            console.print(f"  [dim]{_source_import_safe_record_summary(record)}: {message}[/dim]")


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


def print_memory_audit_events(events: list[object]) -> None:
    if not events:
        info("No memory audit events found")
        return

    table = create_table(
        "Memory Audit",
        "Time",
        "Action",
        "Policy",
        "Scope",
        "Source",
        "Derived",
        expand=False,
    )
    table.columns[0].no_wrap = True
    table.columns[1].no_wrap = True
    for item in events:
        if not isinstance(item, dict):
            continue
        event = cast("dict[str, object]", item)
        created_at = str(event.get("created_at") or "")
        timestamp = created_at.replace("T", " ")[:19]
        scope = str(event.get("memory_scope") or "")
        scope_key = str(event.get("scope_key") or "")
        if scope_key:
            scope = f"{scope}:{scope_key}" if scope else scope_key
        table.add_row(
            timestamp,
            str(event.get("action") or ""),
            _format_policy_state(event.get("policy_allowed")),
            scope,
            _audit_id_summary(event.get("source_ids"), event.get("source_ids_truncated")),
            _audit_id_summary(event.get("derived_ids"), event.get("derived_ids_truncated")),
        )
    console.print(table)


def _preview_state(value: object) -> str:
    return "allowed" if value is True else "denied"


def _access_preview_state(data: dict[str, object]) -> str:
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        state = cast("dict[str, object]", metadata).get("access_state")
        if state in {"allowed", "partial", "denied"}:
            return str(state)
    return _preview_state(data.get("allowed"))


def _preview_target(scope: object, scope_key: object) -> str:
    target = str(scope or "default")
    if scope_key:
        target = f"{target}:{scope_key}"
    return target


def _preview_id_summary(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "-"
    return ", ".join(str(item) for item in value)


def _preview_count(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return "0"


def _preview_audit_id(data: dict[str, object]) -> str:
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    payload = cast("dict[str, object]", metadata)
    for key in ("audit_id", "audit_event_id", "receipt_id"):
        if audit_id := payload.get(key):
            return str(audit_id)
    return ""


def print_promotion_preview(data: dict[str, object]) -> None:
    console.print("\n[bold]Promotion preview[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("State", _preview_state(data.get("allowed")))
    table.add_row("Reason", str(data.get("reason") or ""))
    table.add_row("Candidate", str(data.get("candidate_id") or ""))
    table.add_row("Review", str(data.get("review_state") or ""))
    table.add_row(
        "Target",
        _preview_target(data.get("promote_to_scope"), data.get("promote_to_scope_key")),
    )
    table.add_row("Sources", _preview_id_summary(data.get("raw_source_ids")))
    table.add_row("Reasons", _preview_id_summary(data.get("policy_reasons")))
    if audit_id := _preview_audit_id(data):
        table.add_row("Audit", audit_id)
    console.print(table)


def print_promotion_result(data: dict[str, object]) -> None:
    console.print("\n[bold]Promotion result[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("State", "promoted" if data.get("success") is True else "blocked")
    table.add_row("Reason", str(data.get("reason") or ""))
    table.add_row("Candidate", str(data.get("candidate_id") or ""))
    table.add_row("Review", str(data.get("review_state") or ""))
    table.add_row(
        "Target",
        _preview_target(data.get("memory_scope"), data.get("scope_key")),
    )
    table.add_row("Sources", _preview_id_summary(data.get("raw_source_ids")))
    table.add_row("Policy", _preview_id_summary(data.get("policy_reasons")))
    if promoted_id := data.get("promoted_id"):
        table.add_row("Promoted", str(promoted_id))
    if audit_id := _preview_audit_id(data):
        table.add_row("Audit", audit_id)
    console.print(table)


def print_promotion_autonomy(data: dict[str, object]) -> None:
    console.print("\n[bold]Automatic memory review[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("Outcome", str(data.get("outcome") or ""))
    table.add_row("Action", str(data.get("recommended_action") or ""))
    table.add_row("Applied", "yes" if data.get("applied") is True else "no")
    table.add_row("Reason", str(data.get("reason") or ""))
    table.add_row("Candidate", str(data.get("candidate_id") or ""))
    table.add_row("Review", str(data.get("review_state") or ""))
    table.add_row(
        "Target",
        _preview_target(data.get("promote_to_scope"), data.get("promote_to_scope_key")),
    )
    table.add_row("Sources", _preview_id_summary(data.get("raw_source_ids")))
    table.add_row("Exceptions", _preview_id_summary(data.get("exception_reasons")))
    table.add_row("Policy", _preview_id_summary(data.get("policy_reasons")))
    if promoted_id := data.get("promoted_id"):
        table.add_row("Promoted", str(promoted_id))
    if data.get("dry_run") is True:
        table.add_row("Dry run", "yes")
    if audit_id := _preview_audit_id(data):
        table.add_row("Audit", audit_id)
    console.print(table)


def print_memory_review_drain(data: dict[str, object]) -> None:
    console.print("\n[bold]Memory review drain[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("Mode", "dry-run" if data.get("dry_run") is True else "apply")
    table.add_row("Scanned", _preview_count(data.get("scanned_count")))
    table.add_row("Auto-promote", _preview_count(data.get("auto_promote_count")))
    table.add_row("Applied", _preview_count(data.get("applied_count")))
    table.add_row("Exceptions", _preview_count(data.get("exception_count")))
    table.add_row("Archived", _preview_count(data.get("archived_count")))
    table.add_row("Skipped", _preview_count(data.get("skip_count")))
    table.add_row("Failed", _preview_count(data.get("failed_count")))
    console.print(table)

    results = data.get("results")
    if not isinstance(results, list) or not results:
        return

    result_table = create_table(
        "Drain Results",
        "Candidate",
        "Outcome",
        "Action",
        "State",
        "Reason",
        "Promoted",
        "Archived",
        expand=False,
    )
    for item in results:
        if not isinstance(item, dict):
            continue
        row = cast("dict[str, object]", item)
        result_table.add_row(
            str(row.get("candidate_id") or ""),
            str(row.get("outcome") or ""),
            str(row.get("recommended_action") or ""),
            str(row.get("review_state") or ""),
            str(row.get("reason") or row.get("error") or ""),
            str(row.get("promoted_id") or "-"),
            "yes" if row.get("archived") is True else "no",
        )
    console.print(result_table)


def print_reflection_dream_enqueue(data: dict[str, object], *, dry_run: bool) -> None:
    console.print("\n[bold]Reflection dream cycle[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("Mode", "dry-run" if dry_run else "apply")
    table.add_row("Job", str(data.get("job_id") or ""))
    table.add_row("Function", str(data.get("function") or ""))
    table.add_row("Status", str(data.get("status") or ""))
    table.add_row("Message", str(data.get("message") or ""))
    console.print(table)


def _job_time(job: dict[str, object]) -> str:
    for key in ("finish_time", "start_time", "enqueue_time"):
        if value := job.get(key):
            return str(value).replace("T", " ")[:19]
    return ""


def _event_time(event: dict[str, object]) -> str:
    return str(event.get("created_at") or "").replace("T", " ")[:19]


def _dream_action_label(value: object) -> str:
    action = str(value or "").removeprefix("memory.reflect.")
    if action == "dream_promote":
        return "promote"
    if action == "dream_review":
        return "review"
    return action


def print_reflection_dream_status(data: dict[str, object]) -> None:
    jobs = data.get("jobs")
    events = data.get("events")
    job_items = jobs if isinstance(jobs, list) else []
    event_items = events if isinstance(events, list) else []

    if not job_items and not event_items:
        info("No reflection dream-cycle receipts found")
        return

    if job_items:
        table = create_table(
            "Reflection Dream Runs",
            "Time",
            "Status",
            "Job",
            expand=False,
        )
        for item in job_items:
            if not isinstance(item, dict):
                continue
            job = cast("dict[str, object]", item)
            table.add_row(
                _job_time(job),
                str(job.get("status") or ""),
                str(job.get("job_id") or ""),
            )
        console.print(table)

    if event_items:
        table = create_table(
            "Reflection Dream Receipts",
            "Time",
            "Action",
            "Policy",
            "Scope",
            "Source",
            "Derived",
            expand=False,
        )
        for item in event_items:
            if not isinstance(item, dict):
                continue
            event = cast("dict[str, object]", item)
            scope = str(event.get("memory_scope") or "")
            scope_key = str(event.get("scope_key") or "")
            if scope_key:
                scope = f"{scope}:{scope_key}" if scope else scope_key
            table.add_row(
                _event_time(event),
                _dream_action_label(event.get("action")),
                _format_policy_state(event.get("policy_allowed")),
                scope,
                _audit_id_summary(event.get("source_ids"), event.get("source_ids_truncated")),
                _audit_id_summary(event.get("derived_ids"), event.get("derived_ids_truncated")),
            )
        console.print(table)


def print_share_preview(data: dict[str, object]) -> None:
    console.print("\n[bold]Share preview[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("State", _preview_state(data.get("allowed")))
    table.add_row("Reason", str(data.get("reason") or ""))
    table.add_row("Target", _preview_target(data.get("target_scope"), data.get("target_scope_key")))
    table.add_row("Sources", _preview_id_summary(data.get("source_ids")))
    table.add_row("Visible", _preview_id_summary(data.get("visible_source_ids")))
    table.add_row("Denied", _preview_id_summary(data.get("denied_source_ids")))
    table.add_row("Missing", _preview_id_summary(data.get("missing_source_ids")))
    table.add_row("Redacted", _preview_count(data.get("redacted_count")))
    table.add_row("Hidden relevant", _preview_count(data.get("hidden_but_relevant_count")))
    table.add_row("Reasons", _preview_id_summary(data.get("policy_reasons")))
    if audit_id := _preview_audit_id(data):
        table.add_row("Audit", audit_id)
    console.print(table)


def print_share_result(data: dict[str, object]) -> None:
    console.print("\n[bold]Share result[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("State", "applied" if data.get("applied") is True else "blocked")
    table.add_row("Reason", str(data.get("reason") or ""))
    table.add_row("Target", _preview_target(data.get("target_scope"), data.get("target_scope_key")))
    table.add_row("Sources", _preview_id_summary(data.get("source_ids")))
    table.add_row("Visible", _preview_id_summary(data.get("visible_source_ids")))
    table.add_row("Denied", _preview_id_summary(data.get("denied_source_ids")))
    table.add_row("Promoted", _preview_id_summary(data.get("promoted_ids")))
    table.add_row("Audit", _preview_id_summary(data.get("audit_event_ids")))
    console.print(table)


def print_access_preview(data: dict[str, object]) -> None:
    console.print("\n[bold]Access preview[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("State", _access_preview_state(data))
    table.add_row("Reason", str(data.get("reason") or ""))
    table.add_row(
        "Target",
        _preview_target(data.get("target_principal_type"), data.get("target_principal_id")),
    )
    table.add_row("Spaces", _preview_id_summary(data.get("memory_space_ids")))
    table.add_row("Visible", _preview_id_summary(data.get("visible_source_ids")))
    table.add_row("Denied", _preview_id_summary(data.get("denied_source_ids")))
    table.add_row("Redacted", _preview_count(data.get("redacted_count")))
    table.add_row("Hidden relevant", _preview_count(data.get("hidden_but_relevant_count")))
    table.add_row("Reasons", _preview_id_summary(data.get("policy_reasons")))
    if audit_id := _preview_audit_id(data):
        table.add_row("Audit", audit_id)
    console.print(table)


def _team_count(value: object) -> str:
    return str(len(value)) if isinstance(value, list) else "0"


def print_team_list(data: dict[str, object]) -> None:
    teams = data.get("teams")
    rows = teams if isinstance(teams, list) else []
    table = create_table("Team", "Slug", "Scope", "Memory space")
    for item in rows:
        if not isinstance(item, dict):
            continue
        team = cast("dict[str, object]", item)
        table.add_row(
            str(team.get("name") or ""),
            str(team.get("slug") or ""),
            str(team.get("memory_scope_key") or team.get("id") or ""),
            str(team.get("memory_space_id") or ""),
        )
    console.print(table)


def print_team(data: dict[str, object]) -> None:
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("Team", str(data.get("name") or ""))
    table.add_row("Slug", str(data.get("slug") or ""))
    table.add_row("ID", str(data.get("id") or ""))
    table.add_row("Memory space", str(data.get("memory_space_id") or ""))
    table.add_row("Scope key", str(data.get("memory_scope_key") or ""))
    table.add_row("Members", _team_count(data.get("members")))
    table.add_row("Projects", _team_count(data.get("projects")))
    console.print(table)
