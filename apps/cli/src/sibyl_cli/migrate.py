"""Migrate a project from a personal instance into a team server.

The replay path: raw memory is law, so migration re-submits the verbatim
raw captures to the target through the ordinary authenticated API, as the
caller. The graph pass then carries the project's authored entities and
their links the same way (see migrate_graph). Ownership lands on the
caller's target identity by construction, the target re-projects and
re-embeds server-side, and no cluster or operator access is involved. The
source side reads the local stores directly, which every personal-instance
owner has by definition.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from sibyl_cli import migrate_graph
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import error, info, run_async, success, warn
from sibyl_core.backends.surreal.url_schemes import (
    redact_surreal_url,
    safe_error_detail,
    surreal_http_base_url,
    surreal_url_credentials,
)

app = typer.Typer(help="Migrate data between Sibyl instances")

_LEDGER_DIR = Path.home() / ".sibyl" / "migrations"
_PAGE_SIZE = 200
_MAX_TITLE = 300
_MAX_CONTENT = 500000


_ROUTE_KEYS = ("source_org", "target_context", "target_org_id", "target_project_id")
# Ledgers written before the route carried the target org. Their receipts may
# belong to any org the context was signed in to at the time.
_LEGACY_ROUTE_KEYS = ("source_org", "target_context", "target_project_id")


def _ledger_path(route: dict[str, str], keys: tuple[str, ...] = _ROUTE_KEYS) -> Path:
    safe = "--".join(route[k] for k in keys).replace("/", "_")
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


async def _adopt_legacy_ledger(
    client: Any,
    path: Path,
    route: dict[str, str],
    target_org: dict[str, Any],
    *,
    persist: bool,
) -> dict[str, str]:
    """Carry forward the receipts of an org-less ledger that this org can see.

    Skipping a source memory because a receipt says it was sent is only safe
    when that receipt names a memory in this org. An org-less ledger can mix
    destinations (a context switched orgs between runs, and project ids repeat
    across orgs), so every receipt is checked and only the confirmed ones are
    adopted; the rest are replayed here. The legacy file stays in place for a
    later run into the org its other receipts belong to.

    The check reads the memory's history through the member-readable blame
    route, so any teammate who can migrate can also resume.
    """
    legacy_path = _ledger_path(route, _LEGACY_ROUTE_KEYS)
    if path.exists() or not legacy_path.exists():
        return {}
    legacy_route = {k: route[k] for k in _LEGACY_ROUTE_KEYS}
    receipts = _load_ledger(legacy_path, legacy_route)
    if not receipts:
        return {}
    adopted: dict[str, str] = {}
    for source_id, target_id in receipts.items():
        try:
            await client.memory_blame(str(target_id))
        except SibylClientError as exc:
            if exc.status_code == 404:
                continue
            raise RuntimeError(
                f"could not check receipt {target_id} from {legacy_path.name} "
                f"against the target ({exc}); fix access or move the file aside"
            ) from exc
        adopted[source_id] = target_id
    org_label = f"{target_org['name']} ({target_org['slug']})"
    elsewhere = len(receipts) - len(adopted)
    verb = "Adopted" if persist else "Would adopt"
    info(
        f"{verb} {len(adopted)} of {len(receipts)} receipts from {legacy_path.name} "
        f"for {org_label}"
        + (f"; {elsewhere} are not in this org and will be replayed" if elsewhere else "")
    )
    if persist and adopted:
        _save_ledger(path, route, adopted)
    return adopted


async def _resolve_target_org(client: Any) -> dict[str, Any]:
    """The org the target context's credentials act in, as the server sees it."""
    me = await client.get("/auth/me")
    organization = me.get("organization") or {}
    org_id = str(organization.get("id") or "")
    if not org_id:
        raise RuntimeError("the target server did not report an organization for this login")
    listing = await client.list_orgs()
    listed = next(
        (org for org in listing.get("orgs") or [] if str(org.get("id")) == org_id),
        {},
    )
    return {
        "id": org_id,
        "slug": str(organization.get("slug") or listed.get("slug") or ""),
        "name": str(organization.get("name") or listed.get("name") or org_id),
        "is_personal": bool(listed.get("is_personal")),
    }


_DEFAULT_SOURCE_CREDENTIAL = "root"


def _source_credentials(
    surreal_url: str, username: str | None, password: str | None
) -> tuple[str, str]:
    """Explicit arguments first, then the URL's userinfo, then root:root."""
    from_url = surreal_url_credentials(surreal_url)
    if username is None:
        username = from_url[0] if from_url else _DEFAULT_SOURCE_CREDENTIAL
    if password is None:
        password = from_url[1] if from_url else _DEFAULT_SOURCE_CREDENTIAL
    return username, password


def _source_sql(
    *,
    surreal_url: str,
    username: str | None,
    password: str | None,
    statement: str,
    namespace: str = "sibyl_content",
    database: str = "content",
) -> list[Any]:
    """Run one read-only statement against a local store (the content store by default)."""
    # Built without userinfo, so an HTTP error that quotes the URL cannot
    # carry a password; credentials travel only through `auth`.
    base = surreal_http_base_url(surreal_url)
    if base is None:
        raise ValueError(
            f"Source SurrealDB URL must be a server URL (ws, wss, http, or https), "
            f"not {redact_surreal_url(surreal_url)}"
        )
    failure: RuntimeError | None = None
    try:
        response = httpx.post(
            f"{base}/sql",
            content=statement,
            auth=_source_credentials(surreal_url, username, password),
            headers={
                "Accept": "application/json",
                "Content-Type": "text/plain",
                "surreal-ns": namespace,
                "surreal-db": database,
            },
            timeout=60.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # httpx quotes the request URL, whose path can carry a secret. The
        # replacement is raised outside this block so the original is not
        # chained onto it.
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f"HTTP {exc.response.status_code}"
        else:
            detail = safe_error_detail(exc, base) or type(exc).__name__
        failure = RuntimeError(
            f"Source SurrealDB request to {redact_surreal_url(surreal_url)} failed: {detail}"
        )
    if failure is not None:
        raise failure
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
    username: str | None,
    password: str | None,
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


_GRAPH_PAGE = 500
_UUID_HEX = 32


def _graph_namespace(organization_id: str) -> str:
    return "org_" + organization_id.replace("-", "").lower()


def _organization_id_from_namespace(namespace: str) -> str | None:
    hex_id = namespace.removeprefix("org_")
    if len(hex_id) != _UUID_HEX or any(c not in "0123456789abcdef" for c in hex_id):
        return None
    return "-".join((hex_id[:8], hex_id[8:12], hex_id[12:16], hex_id[16:20], hex_id[20:]))


def _graph_sql(
    *, surreal_url: str, username: str | None, password: str | None, namespace: str, statement: str
) -> list[dict[str, Any]]:
    rows = _source_sql(
        surreal_url=surreal_url,
        username=username,
        password=password,
        statement=statement,
        namespace=namespace,
        database="graph",
    )[0]
    return list(rows or [])


def _source_orgs_with_graph(
    *, surreal_url: str, username: str | None, password: str | None, project: str
) -> list[str]:
    """Every source org whose graph holds rows for the project."""
    root = _source_sql(
        surreal_url=surreal_url,
        username=username,
        password=password,
        statement="INFO FOR ROOT;",
    )[0]
    namespaces = sorted((root or {}).get("namespaces") or {})
    found: list[str] = []
    for namespace in namespaces:
        organization_id = _organization_id_from_namespace(namespace)
        if organization_id is None:
            continue
        rows = _graph_sql(
            surreal_url=surreal_url,
            username=username,
            password=password,
            namespace=namespace,
            statement=f"SELECT count() AS n FROM entity WHERE project_id = '{project}' GROUP ALL;",
        )
        if rows and int(rows[0].get("n") or 0) > 0:
            found.append(organization_id)
    return found


def _source_entity(row: dict[str, Any]) -> migrate_graph.SourceEntity:
    attributes = row.get("attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    tags = row.get("tags") or attributes.get("tags") or []
    return migrate_graph.SourceEntity(
        uuid=str(row.get("uuid")),
        entity_type=str(row.get("entity_type") or attributes.get("entity_type") or ""),
        name=str(row.get("name") or attributes.get("name") or ""),
        memory_scope=row.get("memory_scope") or attributes.get("memory_scope"),
        attributes=attributes,
        created_at=str(row["created_at"]) if row.get("created_at") else None,
        updated_at=str(row["updated_at"]) if row.get("updated_at") else None,
        status=row.get("status") or attributes.get("status"),
        priority=row.get("priority") or attributes.get("priority"),
        tags=tuple(str(tag) for tag in tags if tag),
        summary=row.get("summary"),
        content=row.get("content"),
        description=row.get("description"),
    )


def _read_source_graph(
    *,
    surreal_url: str,
    username: str | None,
    password: str | None,
    organization_id: str,
    project: str,
) -> tuple[list[migrate_graph.SourceEntity], list[migrate_graph.SourceEdge]]:
    """The project's authored entities and the edges between them.

    Topics and passages are not read at all: the target re-derives both, and
    passages carry whole memory bodies. Every ORDER BY field is projected,
    since SurrealDB 3.x refuses to order by a field the SELECT leaves out.
    """
    namespace = _graph_namespace(organization_id)
    entities: list[migrate_graph.SourceEntity] = []
    start = 0
    while True:
        rows = _graph_sql(
            surreal_url=surreal_url,
            username=username,
            password=password,
            namespace=namespace,
            statement=(
                "SELECT uuid, entity_type, name, summary, content, description, status, "
                "priority, memory_scope, tags, created_at, updated_at, attributes FROM entity "
                f"WHERE project_id = '{project}' "
                "AND entity_type NOT IN ['topic', 'passage'] "
                f"ORDER BY uuid LIMIT {_GRAPH_PAGE} START {start};"
            ),
        )
        if not rows:
            break
        entities.extend(_source_entity(row) for row in rows)
        start += _GRAPH_PAGE
    edges: list[migrate_graph.SourceEdge] = []
    start = 0
    while True:
        rows = _graph_sql(
            surreal_url=surreal_url,
            username=username,
            password=password,
            namespace=namespace,
            statement=(
                "SELECT id, name, source_id, target_id FROM relates_to "
                f"WHERE in.project_id = '{project}' AND out.project_id = '{project}' "
                "AND name NOT IN ['PART_OF', 'MENTIONS'] "
                f"ORDER BY id LIMIT {_GRAPH_PAGE} START {start};"
            ),
        )
        if not rows:
            break
        edges.extend(
            migrate_graph.SourceEdge(
                name=str(row.get("name") or ""),
                source_id=str(row.get("source_id") or ""),
                target_id=str(row.get("target_id") or ""),
            )
            for row in rows
        )
        start += _GRAPH_PAGE
    return entities, edges


def _graph_ledger_path(route: dict[str, str]) -> Path:
    return _ledger_path(route).with_suffix(".graph.json")


def _load_graph_ledger(
    path: Path, route: dict[str, str]
) -> tuple[dict[str, str], dict[str, str], dict[str, dict[str, Any]]]:
    """What a previous run landed: target ids, task statuses, rows missing links."""
    if not path.exists():
        return {}, {}, {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}, {}
    if not isinstance(data, dict):
        return {}, {}, {}
    if data.get("route") != route:
        raise RuntimeError(
            f"ledger {path} belongs to a different migration route; "
            "move it aside or pass a different target"
        )
    ids, statuses, partial = data.get("ids"), data.get("statuses"), data.get("partial")
    pending: dict[str, dict[str, Any]] = {}
    for origin, entry in (partial if isinstance(partial, dict) else {}).items():
        # An entry without a digest cannot prove the row is unedited.
        if isinstance(entry, dict):
            pending[origin] = entry
        elif isinstance(entry, list):
            pending[origin] = {"missing": entry, "digest": None}
    return (
        ids if isinstance(ids, dict) else {},
        statuses if isinstance(statuses, dict) else {},
        pending,
    )


def _save_graph_ledger(
    path: Path,
    route: dict[str, str],
    ids: dict[str, str],
    statuses: dict[str, str],
    partial: dict[str, dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"route": route, "ids": ids, "statuses": statuses, "partial": partial},
        indent=1,
        sort_keys=True,
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _report_graph_plan(plan: migrate_graph.GraphPlan, *, resumed: int, share_private: bool) -> None:
    by_type: dict[str, int] = {}
    by_scope: dict[str, int] = {}
    for node in plan.entities:
        by_type[node.source.entity_type] = by_type.get(node.source.entity_type, 0) + 1
        scope = node.scope or "unscoped"
        by_scope[scope] = by_scope.get(scope, 0) + 1
    info(
        f"Graph: {len(plan.entities)} entities in {len(plan.layers)} ordered layers "
        f"({resumed} already in the ledger)"
    )
    info(
        "  " + ", ".join(f"{n} {kind}" for kind, n in sorted(by_type.items(), key=lambda i: -i[1]))
    )
    info("  scope: " + ", ".join(f"{n} {scope}" for scope, n in sorted(by_scope.items())))
    if by_scope.get("private") and not share_private:
        info(
            "  private rows stay private to you; --share-private makes them visible to the project"
        )
    if plan.kept_private:
        info(
            f"  {plan.kept_private} row(s) flagged as holding credentials or tokens stay "
            "private whatever the flags"
        )
    if plan.edge_counts:
        info(
            "  links: "
            + ", ".join(
                f"{n} {name}" for name, n in sorted(plan.edge_counts.items(), key=lambda i: -i[1])
            )
        )
    for reason, count in sorted(plan.skipped.items()):
        info(f"  left for the target to re-derive or already there: {count} {reason}")
    for line in plan.dropped_edges[:10]:
        warn(f"  dropped link {line}")


async def _migrate_graph(
    target: Any,
    *,
    route: dict[str, str],
    organization_id: str,
    project: str,
    target_project_id: str,
    dry_run: bool,
    share_private: bool,
    limit: int | None,
    surreal_url: str,
    username: str | None,
    password: str | None,
) -> list[str]:
    entities, edges = _read_source_graph(
        surreal_url=surreal_url,
        username=username,
        password=password,
        organization_id=organization_id,
        project=project,
    )
    plan = migrate_graph.build_plan(
        entities, edges, project=project, share_private=share_private
    ).limited(limit)
    ledger_file = _graph_ledger_path(route)
    ids, statuses, partial = _load_graph_ledger(ledger_file, route)
    planned = {node.source.uuid for node in plan.entities}
    _report_graph_plan(plan, resumed=len(planned & set(ids)), share_private=share_private)
    if dry_run:
        success(f"Would create {len(planned - set(ids))} graph entities")
        return []
    outcome = await migrate_graph.execute_plan(
        target,
        plan,
        ids=ids,
        statuses=statuses,
        partial=partial,
        target_project_id=target_project_id,
        origin_org=organization_id,
        save=lambda: _save_graph_ledger(ledger_file, route, ids, statuses, partial),
        log=info,
    )
    success(
        f"Created {outcome.created} graph entities ({outcome.resumed} already in the ledger"
        + (f", {outcome.relinked} re-written to add links" if outcome.relinked else "")
        + (
            f", {outcome.adopted} linked to the team's existing container"
            if outcome.adopted
            else ""
        )
        + f"), set {outcome.statuses} task statuses"
    )
    for line in outcome.unlinked[:10]:
        warn(f"  {line}")
    return outcome.failures


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
    response = await client.explore(mode="list", types=["project"], limit=200)
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
        str | None,
        typer.Option(
            "--source-surreal-user",
            help="Local SurrealDB username (defaults to the URL's userinfo, else root)",
        ),
    ] = None,
    source_surreal_pass: Annotated[
        str | None,
        typer.Option(
            "--source-surreal-pass",
            envvar="SIBYL_SOURCE_SURREAL_PASS",
            show_default=False,
            help="Local SurrealDB password (defaults to the URL's userinfo, else "
            "root; prefer the SIBYL_SOURCE_SURREAL_PASS environment variable, since "
            "a value on the command line lands in shell history)",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Count and preview without writing"),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Migrate at most N raw memories and N graph entities"),
    ] = None,
    allow_personal_org: Annotated[
        bool,
        typer.Option(
            "--allow-personal-org",
            help="Migrate into your personal org on the target (refused by default, "
            "since a team migration that lands there is invisible to the team)",
        ),
    ] = False,
    graph: Annotated[
        bool,
        typer.Option(
            "--graph/--no-graph",
            help="Also migrate the project's tasks, epics, decisions, and other "
            "authored entities with their links (on by default)",
        ),
    ] = True,
    share_private: Annotated[
        bool,
        typer.Option(
            "--share-private",
            help="Make your private memories in this project visible to the project "
            "on the target (by default they stay private to you)",
        ),
    ] = False,
) -> None:
    """Migrate a project into a team server as yourself.

    Replays the verbatim raw captures for one project scope from the local
    content store through POST /memory/raw, then (unless --no-graph) creates
    the project's authored graph: tasks with their status and learnings,
    epics, decisions, error patterns, procedures and the rest, with the links
    between them. Topics, passages, and mention links are left for the target
    to re-derive. Provenance records each original id and timestamp, and
    ledgers under ~/.sibyl/migrations make re-runs skip everything already
    migrated.
    """

    @run_async
    async def _run() -> None:
        try:
            migrate_graph.validate_project_scope(project)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if source_org:
            try:
                org_id = migrate_graph.validate_organization_id(source_org)
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
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
            candidates = [str(o["organization_id"]) for o in orgs or []]
            if not candidates and graph:
                candidates = _source_orgs_with_graph(
                    surreal_url=source_surreal_url,
                    username=source_surreal_user,
                    password=source_surreal_pass,
                    project=project,
                )
            if not candidates:
                error(f"No project-scoped memories or graph rows found for {project}")
                raise typer.Exit(1)
            if len(candidates) > 1:
                error(
                    "Multiple source orgs carry this project scope; pass "
                    "--source-org to disambiguate: " + ", ".join(candidates)
                )
                raise typer.Exit(1)
            org_id = candidates[0]

        info(f"Source org {org_id}, project scope {project}")

        target = get_client(target_context)
        target_org = await _resolve_target_org(target)
        info(f"Target org: {target_org['name']} ({target_org['slug']})")
        if target_org["is_personal"]:
            if not allow_personal_org:
                error(
                    f"Context '{target_context}' is signed in to your personal org, "
                    "so the team could not see what this migrates."
                )
                info(
                    f"Switch to the team org first: sibyl -C {target_context} org "
                    "switch <team-slug>, or pass --allow-personal-org to migrate there."
                )
                raise typer.Exit(1)
            warn("Migrating into your personal org (--allow-personal-org)")
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
            "target_org_id": target_org["id"],
            "target_project_id": target_project_id,
        }
        ledger_file = _ledger_path(route)
        ledger = await _adopt_legacy_ledger(
            target, ledger_file, route, target_org, persist=not dry_run
        )
        ledger = ledger or _load_ledger(ledger_file, route)

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
        if graph:
            failed.extend(
                await _migrate_graph(
                    target,
                    route=route,
                    organization_id=org_id,
                    project=project,
                    target_project_id=target_project_id,
                    dry_run=dry_run,
                    share_private=share_private,
                    limit=limit,
                    surreal_url=source_surreal_url,
                    username=source_surreal_user,
                    password=source_surreal_pass,
                )
            )
        if failed:
            error(f"{len(failed)} failures:")
            for line in failed[:10]:
                error(f"  {line}")
            raise typer.Exit(1)

    try:
        _run()
    except RuntimeError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
