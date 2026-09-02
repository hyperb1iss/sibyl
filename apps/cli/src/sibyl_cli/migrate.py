"""Migrate raw memories from a personal instance into a team server.

The replay path: raw memory is law, so migration re-submits the verbatim
raw captures to the target through the ordinary authenticated API, as the
caller. Ownership lands on the caller's target identity by construction,
the target re-projects and re-embeds server-side, and no cluster or
operator access is involved. The source side reads the local content
store directly, which every personal-instance owner has by definition.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from sibyl_cli.client import get_client
from sibyl_cli.common import error, info, run_async, success

app = typer.Typer(help="Migrate data between Sibyl instances")

_LEDGER_DIR = Path.home() / ".sibyl" / "migrations"
_PAGE_SIZE = 200
_MAX_TITLE = 300
_MAX_CONTENT = 500000


def _ledger_path(route: dict[str, str]) -> Path:
    safe = "--".join(
        route[k] for k in ("source_org", "target_context", "target_project_id")
    ).replace("/", "_")
    return _LEDGER_DIR / f"{safe}.json"


def _load_ledger(path: Path, route: dict[str, str]) -> dict[str, str]:
    """Load receipts for exactly this migration route.

    The manifest inside the file pins source org, target context, and
    target project; a mismatch means the file belongs to a different
    route and must not suppress writes for this one.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("route") != route:
        raise RuntimeError(
            f"ledger {path} belongs to a different migration route; "
            "move it aside or pass a different target"
        )
    receipts = data.get("receipts")
    return receipts if isinstance(receipts, dict) else {}


def _save_ledger(path: Path, route: dict[str, str], ledger: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"route": route, "receipts": ledger}, indent=1, sort_keys=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _source_sql(
    *,
    surreal_url: str,
    username: str,
    password: str,
    statement: str,
) -> list[Any]:
    """Run one read-only statement against the local content store."""
    base = surreal_url.replace("ws://", "http://").replace("wss://", "https://")
    base = base.removesuffix("/rpc").rstrip("/")
    response = httpx.post(
        f"{base}/sql",
        content=statement,
        auth=(username, password),
        headers={
            "Accept": "application/json",
            "Content-Type": "text/plain",
            "surreal-ns": "sibyl_content",
            "surreal-db": "content",
        },
        timeout=60.0,
    )
    response.raise_for_status()
    payload = response.json()
    results: list[Any] = []
    for item in payload:
        if item.get("status") != "OK":
            raise RuntimeError(f"source query failed: {item.get('result')}")
        results.append(item.get("result"))
    return results


def _fetch_source_page(
    *,
    surreal_url: str,
    username: str,
    password: str,
    organization_id: str,
    scope_key: str,
    start: int,
) -> list[dict[str, Any]]:
    statement = (
        "SELECT uuid, title, raw_content, memory_scope, scope_key, tags, "
        "metadata, provenance, source_id, capture_surface, created_at "
        "FROM raw_captures "
        f"WHERE organization_id = '{organization_id}' "
        "AND memory_scope = 'project' "
        f"AND scope_key = '{scope_key}' "
        f"ORDER BY created_at ASC LIMIT {_PAGE_SIZE} START {start};"
    )
    rows = _source_sql(
        surreal_url=surreal_url,
        username=username,
        password=password,
        statement=statement,
    )[0]
    return rows or []


async def _resolve_target_project(client: Any, wanted: str) -> dict[str, Any] | None:
    if wanted.startswith("project_"):
        # Exact ids resolve directly, so a project beyond the listing
        # window is still reachable.
        try:
            entity = await client.get_entity(wanted)
        except Exception:
            entity = None
        if entity and str(entity.get("id", "")).lower() == wanted.lower():
            return entity
    response = await client.explore(mode="list", types=["project"], limit=500)
    lowered = wanted.lower()
    entities = response.get("entities", [])
    for project in entities:
        if str(project.get("id", "")).lower() == lowered:
            return project
    named = [p for p in entities if str(p.get("name", "")).lower() == lowered]
    if len(named) > 1:
        raise RuntimeError(
            f"target project name '{wanted}' is ambiguous "
            f"({', '.join(str(p.get('id')) for p in named)}); "
            "pass --target-project with the exact id"
        )
    return named[0] if named else None


@app.command("to-team")
def to_team(
    target_context: Annotated[
        str,
        typer.Option(
            "--target-context",
            help="Named context for the team server (create with sibyl config "
            "context, then sibyl auth login against it)",
        ),
    ],
    project: Annotated[
        str,
        typer.Option(
            "--project",
            help="Source project scope key (project_...) to migrate",
        ),
    ],
    target_project: Annotated[
        str | None,
        typer.Option(
            "--target-project",
            help="Target project id or name; defaults to the same name lookup",
        ),
    ] = None,
    source_org: Annotated[
        str | None,
        typer.Option(
            "--source-org",
            help="Source organization UUID (defaults to the only org with rows "
            "for the project scope)",
        ),
    ] = None,
    source_surreal_url: Annotated[
        str,
        typer.Option("--source-surreal-url", help="Local SurrealDB endpoint"),
    ] = "ws://localhost:8000/rpc",
    source_surreal_user: Annotated[
        str,
        typer.Option("--source-surreal-user", help="Local SurrealDB username"),
    ] = "root",
    source_surreal_pass: Annotated[
        str,
        typer.Option(
            "--source-surreal-pass",
            envvar="SIBYL_SOURCE_SURREAL_PASS",
            show_default=False,
            help="Local SurrealDB password (prefer the "
            "SIBYL_SOURCE_SURREAL_PASS environment variable; a value on "
            "the command line lands in shell history)",
        ),
    ] = "root",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Count and preview without writing"),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Migrate at most N memories"),
    ] = None,
) -> None:
    """Replay a project's raw memories into a team server as yourself.

    Reads the verbatim raw captures for one project scope from the local
    content store, then re-submits each to the target through POST
    /memory/raw with provenance recording the original identity and
    timestamps. A ledger under ~/.sibyl/migrations makes re-runs skip
    everything already migrated.
    """

    @run_async
    async def _run() -> None:
        if source_org:
            org_id = source_org
        else:
            orgs = _source_sql(
                surreal_url=source_surreal_url,
                username=source_surreal_user,
                password=source_surreal_pass,
                statement=(
                    "SELECT organization_id, count() AS n FROM raw_captures "
                    "WHERE memory_scope = 'project' AND scope_key = "
                    f"'{project}' GROUP BY organization_id;"
                ),
            )[0]
            if not orgs:
                error(f"No project-scoped raw memories found for {project}")
                raise typer.Exit(1)
            if len(orgs) > 1:
                error(
                    "Multiple source orgs carry this project scope; pass "
                    "--source-org to disambiguate: "
                    + ", ".join(str(o["organization_id"]) for o in orgs)
                )
                raise typer.Exit(1)
            org_id = str(orgs[0]["organization_id"])

        info(f"Source org {org_id}, project scope {project}")

        target = get_client(target_context)
        wanted = target_project or project
        resolved = await _resolve_target_project(target, wanted)
        if resolved is None and target_project is None:
            # Fall back to the source project's display name via the
            # source API (project entities live in the org graph, which
            # the local API serves; the content store does not).
            source = get_client()
            try:
                source_project = await source.get_entity(project)
            except Exception:
                source_project = None
            source_name = str((source_project or {}).get("name") or "").strip()
            if source_name:
                resolved = await _resolve_target_project(target, source_name)
        if resolved is None:
            error(
                f"Target project '{wanted}' not found on {target_context}. "
                "Create it there first (sibyl project create) or pass "
                "--target-project."
            )
            raise typer.Exit(1)
        target_project_id = str(resolved.get("id"))
        info(f"Target project: {resolved.get('name')} ({target_project_id})")

        route = {
            "source_org": org_id,
            "target_context": target_context,
            "target_project_id": target_project_id,
        }
        ledger_file = _ledger_path(route)
        ledger = _load_ledger(ledger_file, route)

        migrated = 0
        skipped = 0
        failed: list[str] = []
        start = 0
        while True:
            rows = _fetch_source_page(
                surreal_url=source_surreal_url,
                username=source_surreal_user,
                password=source_surreal_pass,
                organization_id=org_id,
                scope_key=project,
                start=start,
            )
            if not rows:
                break
            for row in rows:
                original_id = str(row.get("uuid"))
                if original_id in ledger:
                    skipped += 1
                    continue
                if limit is not None and migrated >= limit:
                    break
                if dry_run:
                    migrated += 1
                    continue
                raw_content = str(row.get("raw_content") or "")
                if not raw_content.strip():
                    failed.append(f"{original_id}: empty content, not migrated")
                    continue
                if len(raw_content) > _MAX_CONTENT:
                    failed.append(
                        f"{original_id}: content exceeds the API limit "
                        f"({len(raw_content)} > {_MAX_CONTENT}); migrate "
                        "this record manually"
                    )
                    continue
                title = str(row.get("title") or "")
                provenance = dict(row.get("provenance") or {})
                provenance["migration"] = {
                    "origin_org": org_id,
                    "origin_raw_id": original_id,
                    "origin_created_at": str(row.get("created_at")),
                    "origin_capture_surface": row.get("capture_surface"),
                    "tool": "sibyl migrate to-team",
                }
                if len(title) > _MAX_TITLE:
                    provenance["migration"]["origin_title"] = title
                    title = title[: _MAX_TITLE - 1] + "…"
                # The retrieval layer prefers metadata.project_id over the
                # scope key (_search_candidates), so a copied source
                # project id would misroute or hide the memory on the
                # target.
                metadata = dict(row.get("metadata") or {})
                if metadata.get("project_id"):
                    provenance["migration"]["origin_metadata_project_id"] = str(
                        metadata["project_id"]
                    )
                    metadata["project_id"] = target_project_id
                try:
                    response = await target.remember_raw_memory(
                        title=title,
                        raw_content=raw_content,
                        source_id=str(row.get("source_id") or "") or f"migrated:{original_id}",
                        memory_scope="project",
                        scope_key=target_project_id,
                        tags=list(row.get("tags") or []),
                        metadata=metadata,
                        provenance=provenance,
                        capture_surface="migration",
                    )
                except Exception as exc:
                    failed.append(f"{original_id}: {exc}")
                    continue
                ledger[original_id] = str(response.get("id") or response.get("uuid") or "ok")
                _save_ledger(ledger_file, route, ledger)
                migrated += 1
                if migrated % 25 == 0:
                    info(f"  {migrated} migrated...")
            if limit is not None and migrated >= limit:
                break
            start += _PAGE_SIZE
            await asyncio.sleep(0)

        if not dry_run:
            _save_ledger(ledger_file, route, ledger)

        verb = "Would migrate" if dry_run else "Migrated"
        success(f"{verb} {migrated} raw memories ({skipped} already in ledger)")
        if failed:
            error(f"{len(failed)} failures:")
            for line in failed[:10]:
                error(f"  {line}")
            raise typer.Exit(1)

    _run()
