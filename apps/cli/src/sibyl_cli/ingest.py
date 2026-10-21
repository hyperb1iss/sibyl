"""Source ingestion CLI commands."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from sibyl_cli import agent_memories
from sibyl_cli.client import SibylClientError, get_client, resolve_client_context_name
from sibyl_cli.common import (
    CORAL,
    console,
    create_table,
    error,
    handle_client_error,
    info,
    print_json,
    run_async,
    success,
    warn,
)

app = typer.Typer(
    name="ingest",
    help="Import local agent memories and source archives",
    no_args_is_help=True,
)

_TERMINAL_STATUSES = {"completed", "failed", "canceled"}


def _resolve_source_path(source: Path) -> str:
    path = source.expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.is_symlink():
        error(f"Transcript source cannot be a symlink: {path}")
        raise typer.Exit(1)
    path = path.resolve()
    if not path.exists():
        error(f"Transcript source not found: {path}")
        raise typer.Exit(1)
    return str(path)


def _scope_label(data: dict[str, object]) -> str:
    scope = str(data.get("target_memory_scope") or "private")
    if scope_key := data.get("target_scope_key"):
        return f"{scope}:{scope_key}"
    return scope


def _progress(data: dict[str, object]) -> dict[str, object]:
    progress = data.get("progress")
    return cast("dict[str, object]", progress) if isinstance(progress, dict) else {}


def _print_import_status(data: dict[str, object]) -> None:
    progress = _progress(data)
    table = create_table("Source Import", "Field", "Value", expand=False)
    table.add_row("Import Id", str(data.get("import_id") or ""))
    table.add_row("Status", str(data.get("status") or ""))
    table.add_row("Adapter", str(data.get("adapter_name") or ""))
    table.add_row("Source", str(data.get("source_identity") or ""))
    table.add_row("Target scope", _scope_label(data))
    table.add_row("Imported", str(progress.get("imported_count") or 0))
    table.add_row("Deduped", str(progress.get("dedupe_count") or 0))
    table.add_row("Skipped", str(progress.get("skipped_count") or 0))
    table.add_row("Errors", str(progress.get("error_count") or 0))
    console.print(table)

    raw_memory_ids = data.get("raw_memory_ids")
    if isinstance(raw_memory_ids, list) and raw_memory_ids:
        console.print("\n[bold]Raw memory receipts[/bold]")
        for raw_memory_id in raw_memory_ids[:18]:
            console.print(f"  [{CORAL}]{raw_memory_id}[/{CORAL}]")


async def _wait_for_import(
    client: Any,
    import_id: str,
    *,
    poll_interval: float,
    timeout: float | None,
) -> dict[str, object]:
    started_at = time.monotonic()
    while True:
        data = await client.ingestion_source_import_status(import_id)
        if str(data.get("status") or "") in _TERMINAL_STATUSES:
            return data
        if timeout is not None and time.monotonic() - started_at >= timeout:
            return data
        await asyncio.sleep(poll_interval)


async def _start_transcript_import(
    *,
    adapter_name: str,
    source: Path,
    scope: str,
    scope_key: str | None,
    source_identity: str | None,
    batch_size: int,
    drain: bool,
    poll_interval: float,
    timeout: float | None,
    json_out: bool,
) -> None:
    source_uri = _resolve_source_path(source)
    options: dict[str, object] = {}
    if source_identity:
        options["source_identity"] = source_identity

    try:
        async with get_client() as client:
            data = await client.start_source_import(
                source_uri=source_uri,
                adapter_name=adapter_name,
                target_memory_scope=scope,
                target_scope_key=scope_key,
                options=options,
                batch_size=batch_size,
                promotion_preview_approved=False,
            )
            if drain:
                import_id = str(data.get("import_id") or "")
                if import_id:
                    if not json_out:
                        info(f"Waiting for import drain {import_id}")
                    data = await _wait_for_import(
                        client,
                        import_id,
                        poll_interval=poll_interval,
                        timeout=timeout,
                    )
            status = str(data.get("status") or "")
            if json_out:
                print_json(data)
                if status in {"failed", "canceled"}:
                    raise typer.Exit(1)
                return

            if status == "completed":
                success("Import completed")
            elif status in {"failed", "canceled"}:
                _print_import_status(data)
                error(f"Import {status}")
                raise typer.Exit(1)
            elif drain and status not in _TERMINAL_STATUSES:
                info("Import drain is still running")
            else:
                success("Import queued")
            _print_import_status(data)
    except SibylClientError as exc:
        handle_client_error(exc, invalid_request_label="Import rejected")


@app.command("claude-code")
def ingest_claude_code(
    source: Annotated[Path, typer.Argument(help="Claude Code JSONL file or directory")],
    scope: Annotated[str, typer.Option("--scope", help="Target memory scope")] = "private",
    scope_key: Annotated[
        str | None,
        typer.Option("--scope-key", help="Target scope key for project/team/shared scopes"),
    ] = None,
    source_identity: Annotated[
        str | None,
        typer.Option("--source-identity", help="Stable identity for moved transcript exports"),
    ] = None,
    batch_size: Annotated[int, typer.Option("--batch-size", help="Import batch size")] = 100,
    drain: Annotated[bool, typer.Option("--drain", help="Wait for the background drain")] = False,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", help="Seconds between drain status checks"),
    ] = 1.0,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Maximum seconds to wait when draining"),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output for scripting")
    ] = False,
) -> None:
    """Import Claude Code transcript JSONL."""

    @run_async
    async def _run() -> None:
        await _start_transcript_import(
            adapter_name="claude_code_jsonl",
            source=source,
            scope=scope,
            scope_key=scope_key,
            source_identity=source_identity,
            batch_size=batch_size,
            drain=drain,
            poll_interval=poll_interval,
            timeout=timeout,
            json_out=json_out,
        )

    _run()


@app.command("codex")
def ingest_codex(
    source: Annotated[Path, typer.Argument(help="Codex JSONL file or directory")],
    scope: Annotated[str, typer.Option("--scope", help="Target memory scope")] = "private",
    scope_key: Annotated[
        str | None,
        typer.Option("--scope-key", help="Target scope key for project/team/shared scopes"),
    ] = None,
    source_identity: Annotated[
        str | None,
        typer.Option("--source-identity", help="Stable identity for moved transcript exports"),
    ] = None,
    batch_size: Annotated[int, typer.Option("--batch-size", help="Import batch size")] = 100,
    drain: Annotated[bool, typer.Option("--drain", help="Wait for the background drain")] = False,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", help="Seconds between drain status checks"),
    ] = 1.0,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Maximum seconds to wait when draining"),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output for scripting")
    ] = False,
) -> None:
    """Import Codex transcript JSONL."""

    @run_async
    async def _run() -> None:
        await _start_transcript_import(
            adapter_name="codex_jsonl",
            source=source,
            scope=scope,
            scope_key=scope_key,
            source_identity=source_identity,
            batch_size=batch_size,
            drain=drain,
            poll_interval=poll_interval,
            timeout=timeout,
            json_out=json_out,
        )

    _run()


_OUTCOME_LABELS = {
    "created": "New",
    "updated": "Changed",
    "relinked": "Attached",
    "unchanged": "Unchanged",
    "retired": "Left alone",
    "failed": "Failed",
}


def agent_memory_paths(claude_dir: Path | None, codex_dir: Path | None) -> tuple[Path, Path, Path]:
    """The Claude Code home, its ``.claude.json``, and the Codex home."""
    home = Path.home()
    claude_home = (claude_dir or home / ".claude").expanduser()
    claude_config = claude_home.parent / ".claude.json"
    codex_home = (codex_dir or home / ".codex").expanduser()
    return claude_home, claude_config, codex_home


def _memory_summary(job: agent_memories.MemoryImport) -> dict[str, Any]:
    outcomes = job.outcomes()
    by_agent: dict[str, dict[str, int]] = {}
    for result in outcomes:
        counts = by_agent.setdefault(result.item.memory.agent, dict.fromkeys(_OUTCOME_LABELS, 0))
        counts[result.outcome] += 1
    return {
        "found": len(job.discovery.memories),
        "outcomes": agent_memories.tally(outcomes),
        "by_agent": by_agent,
        "elsewhere": dict(job.plan.elsewhere),
        "skipped_files": [
            {"file": str(skip.file), "reason": skip.reason} for skip in job.discovery.skipped
        ],
        "failures": [
            {"file": str(result.item.memory.file), "error": result.detail}
            for result in outcomes
            if result.outcome == "failed"
        ],
        "attached_to_projects": sum(1 for result in outcomes if result.item.project_id),
    }


def _print_memory_summary(job: agent_memories.MemoryImport, *, title: str) -> None:
    summary = _memory_summary(job)
    table = create_table(title, "Agent", *_OUTCOME_LABELS.values(), expand=False)
    for agent, counts in sorted(summary["by_agent"].items()):
        table.add_row(agent, *(str(counts[key]) for key in _OUTCOME_LABELS))
    console.print(table)
    attached = summary["attached_to_projects"]
    if attached:
        info(f"{attached} memories attach to the Sibyl project their repository is linked to")
    for context, count in sorted(summary["elsewhere"].items()):
        info(
            f"{count} memories come from repositories pinned to context '{context}': "
            f"sibyl -C {context} ingest memories"
        )
    if summary["skipped_files"]:
        info(f"{len(summary['skipped_files'])} files skipped (symlinks, empty, or too large)")
    for failure in summary["failures"][:10]:
        warn(f"{failure['file']}: {failure['error']}")


async def run_memory_import(
    *,
    agents: list[str],
    claude_dir: Path | None,
    codex_dir: Path | None,
    share: bool,
    confirm: Any,
    context_name: str | None = None,
) -> agent_memories.MemoryImport | None:
    """Plan an import, ask ``confirm`` (a callable taking the plan), then apply.

    Imports into ``context_name``, or the context this CLI talks to here.
    Returns None when nothing was found. ``confirm`` returning False stops
    after the preview.
    """
    claude_home, claude_config, codex_home = agent_memory_paths(claude_dir, codex_dir)
    target = context_name or resolve_client_context_name()
    async with get_client(context_name) as client:
        # Clients are cached per context, so restore buffering for whatever
        # else this process sends.
        buffering = client.buffer_writes
        client.buffer_writes = False
        try:
            job = await agent_memories.plan_memory_import(
                client,
                agents=agents,
                claude_home=claude_home,
                claude_config=claude_config,
                codex_home=codex_home,
                target_context=target,
                share=share,
            )
            if not job.discovery.memories:
                return None
            if job.pending and confirm(job):
                await agent_memories.apply_memory_import(client, job)
            return job
        finally:
            client.buffer_writes = buffering


@app.command("memories")
def ingest_memories(
    agent: Annotated[
        list[str] | None,
        typer.Option(
            "--from",
            help="Agent to import from: claude-code or codex (repeatable; default both)",
        ),
    ] = None,
    share: Annotated[
        bool,
        typer.Option(
            "--share",
            help=(
                "Make memories from linked repositories visible to that project "
                "instead of private (personal memories always stay private)"
            ),
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would change without writing")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask before writing")] = False,
    claude_dir: Annotated[
        Path | None,
        typer.Option("--claude-dir", help="Claude Code home (default ~/.claude)"),
    ] = None,
    codex_dir: Annotated[
        Path | None,
        typer.Option("--codex-dir", help="Codex home (default ~/.codex)"),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output for scripting")
    ] = False,
) -> None:
    """Import the memory files Claude Code and Codex keep on this machine.

    Each Claude Code memory file and Codex session summary lands as one raw
    memory carrying its kind (feedback as a rule, a reference as an artifact,
    a session as a session), private by default. A memory attaches to the
    project its repository is linked to. Re-running revises changed files in
    place and skips unchanged ones.
    """
    agents = agent or list(agent_memories.AGENTS)
    unknown = [name for name in agents if name not in agent_memories.AGENTS]
    if unknown:
        error(f"Unknown agent: {', '.join(unknown)} (choose claude-code or codex)")
        raise typer.Exit(1)

    def confirm(job: agent_memories.MemoryImport) -> bool:
        if dry_run:
            return False
        if yes or json_out or not (sys.stdin.isatty() and sys.stdout.isatty()):
            return True
        _print_memory_summary(job, title="Agent Memories (preview)")
        counts = agent_memories.tally(job.preview)
        new = counts["created"] + counts["relinked"]
        visibility = "visible to their linked projects" if share else "private to you"
        return typer.confirm(
            f"Import {new} new and update {counts['updated']} memories, {visibility}?",
            default=True,
        )

    @run_async
    async def _run() -> None:
        try:
            job = await run_memory_import(
                agents=agents,
                claude_dir=claude_dir,
                codex_dir=codex_dir,
                share=share,
                confirm=confirm,
            )
        except SibylClientError as exc:
            handle_client_error(exc, invalid_request_label="Import rejected")
            return
        if job is None:
            if json_out:
                print_json({"found": 0})
            else:
                info("No Claude Code or Codex memory files found on this machine")
            return
        summary = _memory_summary(job)
        if json_out:
            print_json({**summary, "dry_run": dry_run, "applied": bool(job.results)})
        else:
            title = "Agent Memories (dry run)" if dry_run else "Agent Memories"
            _print_memory_summary(job, title=title)
            if job.results:
                success("Imported agent memories")
            elif not job.pending:
                success("Agent memories are already up to date")
        if summary["outcomes"]["failed"]:
            raise typer.Exit(1)

    _run()
