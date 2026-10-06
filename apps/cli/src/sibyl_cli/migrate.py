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
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
import typer

from sibyl_cli import migrate_graph
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import error, info, run_async, success, warn
from sibyl_cli.memory_display import raw_memory_lookup_value
from sibyl_core.backends.surreal.url_schemes import (
    redact_surreal_url,
    safe_error_detail,
    surreal_http_base_url,
    surreal_url_credentials,
)
from sibyl_core.services.content_models import (
    RawMemory,
    raw_memory_recallable,
    raw_memory_unpublished_reflection_candidate,
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


def _route_fingerprint(route: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(route, sort_keys=True).encode()).hexdigest()


async def _bind_route(
    client: Any,
    route: dict[str, str],
    *,
    source_url: str,
    source_project: str,
    require_graph: bool = False,
    require_undo: bool = False,
) -> dict[str, str]:
    identity = await client.get("/auth/replay-identity")
    if "migration_replay_policy_v1" not in identity.get("capabilities", []):
        raise RuntimeError(
            "upgrade the target server before migrating: it does not advertise "
            "protected migration retry handling"
        )
    if require_graph and "migration_graph_writes_v1" not in identity.get("capabilities", []):
        raise RuntimeError(
            "upgrade the target server before migrating graph data: it does not advertise "
            "protected graph creation and additive link writes"
        )
    if require_undo and "migration_guarded_delete_v1" not in identity.get("capabilities", []):
        raise RuntimeError(
            "upgrade the target server before undoing a migration: it does not advertise "
            "deletes guarded by the revision the migration left"
        )
    if (
        not identity.get("server_instance_id")
        or not identity.get("user_id")
        or identity.get("organization_id") != route["target_org_id"]
    ):
        raise RuntimeError("the target server did not confirm this migration's write identity")
    source_base = surreal_http_base_url(source_url)
    if not source_base:
        raise RuntimeError("the source must be a SurrealDB server URL")
    return {
        **route,
        "source_endpoint_id": hashlib.sha256(source_base.encode()).hexdigest(),
        "source_project_id": source_project,
        "target_server_id": str(identity["server_instance_id"]),
        "target_user_id": str(identity["user_id"]),
    }


def _ledger_path(route: dict[str, str], keys: tuple[str, ...] = _ROUTE_KEYS) -> Path:
    if keys == _ROUTE_KEYS and "target_user_id" in route:
        return _LEDGER_DIR / f"{_route_fingerprint(route)}.json"
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


def _save_migration_ledger(path: Path, data: dict[str, Any]) -> None:
    """Persist private retry bodies before allowing their remote writes."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=1, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _save_ledger(path: Path, route: dict[str, str], ledger: dict[str, str]) -> None:
    _save_migration_ledger(path, {"route": route, "receipts": ledger})


def _raw_revisions_path(ledger_file: Path) -> Path:
    return ledger_file.with_suffix(".raw-revisions.json")


def _load_raw_revisions(ledger_file: Path, route: dict[str, str]) -> dict[str, int]:
    """The revision each replayed capture had when it landed, for an undo to compare."""
    try:
        data = json.loads(_raw_revisions_path(ledger_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("route") != route:
        return {}
    revisions = data.get("revisions")
    if not isinstance(revisions, dict):
        return {}
    return {str(k): v for k, v in revisions.items() if type(v) is int and v >= 1}


def _raw_intents_path(ledger_file: Path) -> Path:
    return ledger_file.with_suffix(".raw-intents.json")


def _load_raw_intents(ledger_file: Path, route: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Raw writes sent but not yet confirmed, with the key and body each was sent with."""
    try:
        data = json.loads(_raw_intents_path(ledger_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("route") != route:
        return {}
    intents = data.get("intents")
    if not isinstance(intents, dict):
        return {}
    return {
        str(origin): intent
        for origin, intent in intents.items()
        if isinstance(intent, dict)
        and intent.get("key")
        and isinstance(intent.get("request"), dict)
    }


def _save_raw_intents(
    ledger_file: Path, route: dict[str, str], intents: dict[str, dict[str, Any]]
) -> None:
    _save_migration_ledger(_raw_intents_path(ledger_file), {"route": route, "intents": intents})


def _save_raw_revisions(
    ledger_file: Path, route: dict[str, str], revisions: dict[str, int]
) -> None:
    _save_migration_ledger(
        _raw_revisions_path(ledger_file), {"route": route, "revisions": revisions}
    )


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
    legacy_route = {k: route[k] for k in _ROUTE_KEYS}
    legacy_path = _ledger_path(legacy_route)
    if not legacy_path.exists():
        legacy_route = {k: route[k] for k in _LEGACY_ROUTE_KEYS}
        legacy_path = _ledger_path(route, _LEGACY_ROUTE_KEYS)
    if path.exists() or not legacy_path.exists():
        return {}
    receipts = _load_ledger(legacy_path, legacy_route)
    if not receipts:
        return {}
    adopted: dict[str, str] = {}
    for source_id, target_id in receipts.items():
        try:
            response = await client.memory_blame(str(target_id))
        except SibylClientError as exc:
            if exc.status_code == 404:
                continue
            raise RuntimeError(
                f"could not check receipt {target_id} from {legacy_path.name} "
                f"against the target ({exc}); fix access or move the file aside"
            ) from exc
        source = response.get("source") or {}
        if "target_user_id" in route and (
            source.get("principal_id") != route["target_user_id"]
            or source.get("organization_id") != route["target_org_id"]
            or source.get("scope_key") != route["target_project_id"]
        ):
            continue
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
_UNDO_REFUSED = "undo refused: project maintainer access required"
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Where `sibyl local` publishes its SurrealDB (127.0.0.1:8000 in its compose file).
_LOCAL_INSTALL_PORT = 8000


def _local_install_credentials() -> tuple[str, str] | None:
    """The SurrealDB login `sibyl local` generated on this machine, if there is one."""
    from sibyl_cli.local import SIBYL_LOCAL_ENV

    try:
        text = SIBYL_LOCAL_ENV.read_text(encoding="utf-8")
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and not key.startswith("#"):
            values[key.strip()] = value.strip().strip("\"'")
    password = values.get("SIBYL_SURREAL_PASSWORD")
    if not password:
        return None
    return values.get("SIBYL_SURREAL_USERNAME") or _DEFAULT_SOURCE_CREDENTIAL, password


def _resolve_source_credentials(
    surreal_url: str, username: str | None, password: str | None
) -> tuple[str | None, str | None]:
    """Find the login for a local source when none was given.

    `sibyl local` generates a random SurrealDB password, so root:root fails
    against it, while a development server usually takes root:root. For a
    source at the local install's own address (loopback, port 8000) with no
    credentials in the arguments or the URL, the generated login is tried
    first and root:root second; the first one the server runs a query as is
    used. Any other address never receives the local login.
    """
    if username is not None or password is not None or surreal_url_credentials(surreal_url):
        return username, password
    base = surreal_http_base_url(surreal_url)
    parsed = urlsplit(base) if base else None
    # Only the port the local install publishes: another loopback port can be
    # a tunnel or forward to some other machine.
    if (
        parsed is None
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.port != _LOCAL_INSTALL_PORT
    ):
        return username, password
    local = _local_install_credentials()
    if local is None:
        return username, password
    for candidate in (local, (_DEFAULT_SOURCE_CREDENTIAL, _DEFAULT_SOURCE_CREDENTIAL)):
        try:
            response = httpx.post(
                f"{base}/sql",
                content="RETURN true;",
                auth=candidate,
                headers={"Accept": "application/json", "Content-Type": "text/plain"},
                timeout=10.0,
            )
        except httpx.HTTPError:
            return username, password
        if _accepted(response):
            return candidate
    return username, password


def _accepted(response: httpx.Response) -> bool:
    """Whether the server ran the probe as this login, not merely answered."""
    if response.status_code != 200:
        return False
    try:
        results = response.json()
    except ValueError:
        return False
    return (
        isinstance(results, list)
        and bool(results)
        and isinstance(results[0], dict)
        and results[0].get("status") == "OK"
    )


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
            if exc.response.status_code in {401, 403}:
                detail += (
                    "; pass the local SurrealDB login with --source-surreal-user and "
                    "SIBYL_SOURCE_SURREAL_PASS (a `sibyl local` install keeps it in "
                    "~/.sibyl/local/.env as SIBYL_SURREAL_PASSWORD)"
                )
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
        "metadata, provenance, source_id, capture_surface, created_at, "
        "review_state, revision, deleted_at "
        "FROM raw_captures "
        f"WHERE organization_id = '{organization_id}' "
        "AND memory_scope = 'project' "
        f"AND scope_key = '{scope_key}' "
        f"ORDER BY created_at ASC, uuid ASC LIMIT {_PAGE_SIZE} START {start};"
    )
    rows = _source_sql(
        surreal_url=surreal_url,
        username=username,
        password=password,
        statement=statement,
    )[0]
    return rows or []


def _raw_migratable(row: dict[str, Any]) -> bool:
    if row.get("deleted_at"):
        return False
    memory = RawMemory(
        id=str(row.get("uuid") or ""),
        organization_id="",
        principal_id="",
        source_id=str(row.get("source_id") or ""),
        review_state=str(row.get("review_state") or "pending"),
        revision=int(row.get("revision") or 1),
        metadata=dict(row.get("metadata") or {}),
        capture_surface=row.get("capture_surface"),
    )
    return raw_memory_recallable(memory) and not raw_memory_unpublished_reflection_candidate(memory)


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
    revisions: dict[str, int] | None = None,
    structure: dict[str, dict[str, Any]] | None = None,
    preexisting: set[str] | None = None,
    undoing: set[str] | None = None,
) -> None:
    payload: dict[str, Any] = {"route": route, "ids": ids, "statuses": statuses, "partial": partial}
    if revisions is not None:
        payload["revisions"] = revisions
    if structure is not None:
        payload["structure"] = structure
    if preexisting is not None:
        payload["preexisting"] = sorted(preexisting)
    if undoing is not None:
        payload["undoing"] = sorted(undoing)
    _save_migration_ledger(path, payload)


def _load_graph_extras(
    path: Path, route: dict[str, str]
) -> tuple[dict[str, int], dict[str, dict[str, Any]], set[str], set[str]]:
    """Beyond the receipts: revisions, row structure, adopted rows, rows an undo marked."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}, set(), set()
    if not isinstance(data, dict) or data.get("route") != route:
        return {}, {}, set(), set()
    revisions = data.get("revisions") if isinstance(data.get("revisions"), dict) else {}
    structure = data.get("structure") if isinstance(data.get("structure"), dict) else {}
    preexisting = data.get("preexisting") if isinstance(data.get("preexisting"), list) else []
    return (
        {
            str(origin): revision
            for origin, revision in revisions.items()
            if type(revision) is int and revision >= 1
        },
        {str(origin): shape for origin, shape in structure.items() if isinstance(shape, dict)},
        {str(origin) for origin in preexisting},
        {str(origin) for origin in (data.get("undoing") or []) if isinstance(origin, str)},
    )


def _epoch_path(route: dict[str, str]) -> Path:
    return _ledger_path(route).with_suffix(".epoch.json")


def _key_namespace(route: dict[str, str]) -> str:
    """The prefix of this route's operation keys.

    The server keeps a completed write's receipt and replays it for the same
    key, even after the row is gone. An undo therefore moves the route to a new
    epoch first, so migrating again afterwards writes fresh rows instead of
    replaying receipts for the ones the undo removed.
    """
    fingerprint = _route_fingerprint(route)
    epoch = _load_epoch(route)
    return fingerprint if not epoch else f"{fingerprint}:undo-{epoch}"


def _load_epoch(route: dict[str, str]) -> int:
    try:
        data = json.loads(_epoch_path(route).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict) or data.get("route") != route:
        return 0
    epoch = data.get("epoch")
    return epoch if type(epoch) is int and epoch > 0 else 0


def _advance_epoch(route: dict[str, str]) -> None:
    _save_migration_ledger(_epoch_path(route), {"route": route, "epoch": _load_epoch(route) + 1})


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
    ledger_file = _graph_ledger_path(route)
    ids, statuses, partial = _load_graph_ledger(ledger_file, route)
    revisions, structure, preexisting, undoing = _load_graph_extras(ledger_file, route)
    plan = migrate_graph.build_plan(
        entities, edges, project=project, share_private=share_private
    ).limited(limit, done=set(ids))
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
        operation_namespace=_key_namespace(route),
        revisions=revisions,
        structure=structure,
        preexisting=preexisting,
        undoing=undoing,
        save=lambda: _save_graph_ledger(
            ledger_file, route, ids, statuses, partial, revisions, structure, preexisting, undoing
        ),
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


async def _undo_graph(target: Any, *, route: dict[str, str], dry_run: bool) -> list[str]:
    """Remove the graph rows this route's migration created and nobody has touched.

    Reads only the ledger, so it follows what was actually migrated even after
    the source changed or is gone.
    """
    ledger_file = _graph_ledger_path(route)
    ids, statuses, partial = _load_graph_ledger(ledger_file, route)
    if not ids:
        info("Graph: nothing to undo for this route")
        return []
    revisions, structure, preexisting, undoing = _load_graph_extras(ledger_file, route)
    outcome = await migrate_graph.undo_plan(
        target,
        structure=structure,
        ids=ids,
        revisions=revisions,
        statuses=statuses,
        partial=partial,
        dry_run=dry_run,
        undoing=undoing,
        project_id=route.get("target_project_id"),
        save=lambda: _save_graph_ledger(
            ledger_file, route, ids, statuses, partial, revisions, structure, preexisting, undoing
        ),
        log=info,
    )
    if outcome.refused:
        error(
            "The team server refused the deletes: undoing needs project maintainer access. "
            "Ask a maintainer of the project to grant it, then run the undo again."
        )
        return [_UNDO_REFUSED]
    if outcome.unresolved:
        info(
            f"{outcome.unresolved} creates were sent without a confirmed answer; "
            "the undo checks them first and removes any that landed"
        )
    if outcome.unresolved_unkeyed:
        warn(
            f"{outcome.unresolved_unkeyed} creates from an older run were sent without a "
            "confirmed answer and cannot be checked; if they landed, they stay"
        )
    verb = "Would remove" if dry_run else "Removed"
    success(
        f"{verb} {outcome.removed} graph entities"
        + (f" ({outcome.gone} already gone)" if outcome.gone else "")
    )
    for label, kept in (
        ("changed on the team server since migration", outcome.kept_edited),
        ("still linked from a kept row", outcome.kept_linked),
        (
            "that someone else edited or that a row outside this migration links to",
            outcome.kept_shared,
        ),
        ("not undoable by this run", outcome.kept_unrecorded),
    ):
        if kept:
            warn(f"Kept {len(kept)} {label}:")
            for line in kept[:5]:
                warn(f"  {line}")
    return outcome.failures


async def _undo_raw(
    target: Any,
    *,
    ledger_file: Path,
    route: dict[str, str],
    ledger: dict[str, str],
    revisions: dict[str, int],
    dry_run: bool,
    intents: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Delete the raw captures this route replayed, through the memory lifecycle.

    A capture goes only at the revision it landed at, so one corrected or
    edited since stays; one that landed on an existing capture, or with no
    recorded revision, was not created by this migration and stays too.
    """
    failures: list[str] = []
    removed = 0
    gone = 0
    kept: list[str] = []
    intents = intents if intents is not None else {}
    # A raw write sent without a confirmed answer may have landed. Replay it
    # under its own key (the original receipt, or the capture created now) so
    # the undo removes it too.
    unconfirmed = {o: i for o, i in intents.items() if o not in ledger}
    if dry_run and unconfirmed:
        info(
            f"{len(unconfirmed)} raw writes were sent without a confirmed answer; the undo checks them first"
        )
    for origin, intent in unconfirmed.items() if not dry_run else ():
        try:
            response = await target.remember_raw_memory(
                **intent["request"], _idempotency_key=intent["key"]
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if isinstance(status, int) and 400 <= status < 500 and status not in {401, 403, 409}:
                # The server refused the write outright, so it never landed.
                intents.pop(origin, None)
                _save_raw_intents(ledger_file, route, intents)
                continue
            failures.append(f"raw {origin}: could not confirm an unfinished write ({exc})")
            continue
        ledger[origin] = str(response.get("id") or response.get("uuid") or "ok")
        if type(response.get("revision")) is int:
            revisions[origin] = response["revision"]
        intents.pop(origin, None)
        _save_raw_revisions(ledger_file, route, revisions)
        _save_ledger(ledger_file, route, ledger)
        _save_raw_intents(ledger_file, route, intents)
    for origin, target_id in list(ledger.items()):
        # The corrections API takes the bare capture id, as `sibyl correct` sends it.
        source_id = raw_memory_lookup_value(target_id)
        revision = revisions.get(origin)
        if revision != 1:
            kept.append(f"raw {origin}: the migration did not create it, or left no record of it")
            continue
        try:
            preview = await target.correct_memory(
                source_id,
                action="delete",
                reason="sibyl migrate to-team --undo",
                expected_revision=revision,
                preview=True,
            )
        except SibylClientError as exc:
            if exc.status_code == 404:
                gone += 1
                if not dry_run:
                    ledger.pop(origin, None)
                    revisions.pop(origin, None)
                continue
            if exc.status_code == 409:
                kept.append(f"raw {origin}: changed on the team server since it was migrated")
                continue
            failures.append(f"raw {origin}: {exc}")
            continue
        if not preview.get("allowed"):
            failures.append(f"raw {origin}: the team server refused to delete it")
            continue
        if dry_run:
            removed += 1
            continue
        try:
            await target.correct_memory(
                source_id,
                action="delete",
                reason="sibyl migrate to-team --undo",
                expected_revision=revision,
            )
        except SibylClientError as exc:
            if exc.status_code == 409:
                kept.append(f"raw {origin}: changed on the team server during the undo")
            else:
                failures.append(f"raw {origin}: {exc}")
            continue
        ledger.pop(origin, None)
        revisions.pop(origin, None)
        removed += 1
        # Saved per capture: a migration after an interrupted undo must not
        # skip a capture the undo already removed.
        _save_ledger(ledger_file, route, ledger)
        _save_raw_revisions(ledger_file, route, revisions)
    if not dry_run:
        _save_ledger(ledger_file, route, ledger)
        _save_raw_revisions(ledger_file, route, revisions)
    success(f"{'Would remove' if dry_run else 'Removed'} {removed} raw memories")
    if gone:
        warn(f"{gone} raw memories in the ledger were already gone from the team server")
    if kept:
        warn(f"Kept {len(kept)} raw memories:")
        for line in kept[:5]:
            warn(f"  {line}")
    return failures


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
    undo: Annotated[
        bool,
        typer.Option(
            "--undo",
            help="Remove what this migration created on the target and nobody has "
            "changed since (combine with --dry-run to preview)",
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
        nonlocal source_surreal_user, source_surreal_pass
        try:
            migrate_graph.validate_project_scope(project)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        source_surreal_user, source_surreal_pass = _resolve_source_credentials(
            source_surreal_url, source_surreal_user, source_surreal_pass
        )
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
        route = await _bind_route(
            target,
            route,
            source_url=source_surreal_url,
            source_project=project,
            require_graph=graph,
            require_undo=undo,
        )
        unbound_route = {key: route[key] for key in _ROUTE_KEYS}
        if graph and _graph_ledger_path(unbound_route).exists():
            raise RuntimeError(
                "an older graph migration ledger has no verified server or author identity; "
                "its target rows must be verified before resuming"
            )
        ledger_file = _ledger_path(route)
        ledger = await _adopt_legacy_ledger(
            target, ledger_file, route, target_org, persist=not dry_run
        )
        ledger = ledger or _load_ledger(ledger_file, route)
        raw_revisions = _load_raw_revisions(ledger_file, route)
        raw_intents = _load_raw_intents(ledger_file, route)

        if undo:
            failed_undo: list[str] = []
            if not dry_run:
                # Before the first delete, so even an interrupted undo leaves a
                # later migration writing fresh rows.
                _advance_epoch(route)
            if graph:
                failed_undo.extend(await _undo_graph(target, route=route, dry_run=dry_run))
            if _UNDO_REFUSED in failed_undo:
                # A refused undo changes nothing, so it can run whole once
                # access is granted instead of leaving the project half undone.
                info("Raw memories were left in place too.")
            else:
                failed_undo.extend(
                    await _undo_raw(
                        target,
                        ledger_file=ledger_file,
                        route=route,
                        ledger=ledger,
                        revisions=raw_revisions,
                        dry_run=dry_run,
                        intents=raw_intents,
                    )
                )
            if failed_undo:
                error(f"{len(failed_undo)} failures:")
                for line in failed_undo[:10]:
                    error(f"  {line}")
                raise typer.Exit(1)
            return

        migrated = 0
        skipped = 0
        excluded = 0
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
                if not _raw_migratable(row):
                    excluded += 1
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
                if dry_run:
                    migrated += 1
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
                # A write sent before keeps its key and body, so a retry, even
                # after an undo moved the route's keys, gets the original receipt.
                intent = raw_intents.get(original_id) or {
                    "key": "migration-raw:"
                    + hashlib.sha256(f"{_key_namespace(route)}:{original_id}".encode()).hexdigest(),
                    "request": {
                        "title": title,
                        "raw_content": raw_content,
                        "source_id": str(row.get("source_id") or "") or f"migrated:{original_id}",
                        "memory_scope": "project",
                        "scope_key": target_project_id,
                        "tags": list(row.get("tags") or []),
                        "metadata": metadata,
                        "provenance": provenance,
                        "capture_surface": "migration",
                    },
                }
                raw_intents[original_id] = intent
                _save_raw_intents(ledger_file, route, raw_intents)
                try:
                    response = await target.remember_raw_memory(
                        **intent["request"], _idempotency_key=intent["key"]
                    )
                except Exception as exc:
                    failed.append(f"{original_id}: {exc}")
                    continue
                ledger[original_id] = str(response.get("id") or response.get("uuid") or "ok")
                raw_intents.pop(original_id, None)
                _save_raw_intents(ledger_file, route, raw_intents)
                if type(response.get("revision")) is int:
                    raw_revisions[original_id] = response["revision"]
                    _save_raw_revisions(ledger_file, route, raw_revisions)
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
        if excluded:
            info(f"Left {excluded} raw memories excluded by their source lifecycle")
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
