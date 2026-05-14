from __future__ import annotations

import argparse
import ast
import difflib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TextIO

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "docs/research/rust-port/INVENTORY.md"
GRAPHITI_EXIT_INVENTORY_PATH = REPO_ROOT / "docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md"
APP_PATH = REPO_ROOT / "apps/api/src/sibyl/api/app.py"
MODELS_PATH = REPO_ROOT / "apps/api/src/sibyl/db/models.py"
PYPROJECT_EXCLUDED_PARTS = {
    ".git",
    ".moon",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "build",
    "dist",
    "node_modules",
}
SOURCE_ROOTS = [
    REPO_ROOT / "apps/api/src",
    REPO_ROOT / "packages/python/sibyl-core/src",
]
HTTP_METHOD_DECORATORS = {
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "trace",
}
SQL_IMPORT_PREFIXES = ("sqlalchemy", "sqlmodel")
GRAPHITI_IMPORT_PREFIXES = ("graphiti", "graphiti_core")
SQL_SESSION_IMPORTS = {
    "AsyncSession",
    "Session",
    "async_sessionmaker",
    "sessionmaker",
}
SQL_QUERY_IMPORTS = {
    "delete",
    "insert",
    "select",
    "text",
    "update",
}
SQL_SESSION_CALLS = {
    "add",
    "commit",
    "delete",
    "exec",
    "execute",
    "get",
    "refresh",
    "rollback",
    "scalar",
    "scalars",
}
LEGACY_DEPENDENCY_NAMES = {
    "alembic",
    "asyncpg",
    "pgvector",
    "sqlalchemy",
    "sqlmodel",
}
GRAPH_DEPENDENCY_NAMES = {"graphiti-core"}
TARGET_DEPENDENCY_NAMES = {"surrealdb"}
GraphitiSurfaceClass = Literal["admin", "archived_docs", "compatibility", "migration", "test"]


def _is_repo_pyproject(path: Path) -> bool:
    relative_parts = path.relative_to(REPO_ROOT).parts
    return not any(part in PYPROJECT_EXCLUDED_PARTS for part in relative_parts)


PYPROJECT_PATHS = tuple(
    sorted(path for path in REPO_ROOT.rglob("pyproject.toml") if _is_repo_pyproject(path))
)


@dataclass(frozen=True, slots=True)
class HttpRoute:
    method: str
    path: str
    handler: str


@dataclass(frozen=True, slots=True)
class WebSocketRouteRecord:
    path: str
    handler: str


@dataclass(frozen=True, slots=True)
class McpDecoratorRecord:
    name: str
    location: str
    target: str | None = None


@dataclass(frozen=True, slots=True)
class SqlUsageRecord:
    path: str
    session_imports: tuple[str, ...]
    query_imports: tuple[str, ...]
    session_calls: tuple[str, ...]
    query_calls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GraphitiImportRecord:
    path: str
    imports: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GraphitiCompatibilityRecord:
    path: str
    classification: GraphitiSurfaceClass
    owner: str
    criteria: str


@dataclass(frozen=True, slots=True)
class DependencyRecord:
    project: str
    dependency: str
    classification: str
    scope: str


@dataclass(frozen=True, slots=True)
class RuntimeSurface:
    rest_routers: tuple[str, ...]
    top_level_http_routes: tuple[HttpRoute, ...]
    websocket_routes: tuple[WebSocketRouteRecord, ...]
    mcp_tools: tuple[McpDecoratorRecord, ...]
    mcp_resources: tuple[McpDecoratorRecord, ...]
    sqlmodel_tables: tuple[str, ...]
    raw_sql_usage: tuple[SqlUsageRecord, ...]
    session_storage_usage: tuple[SqlUsageRecord, ...]
    graphiti_imports: tuple[GraphitiImportRecord, ...]
    dependencies: tuple[DependencyRecord, ...]


GRAPHITI_COMPATIBILITY_ALLOWLIST = (
    GraphitiCompatibilityRecord(
        path="packages/python/sibyl-core/src/sibyl_core/backends/surreal/driver.py",
        classification="compatibility",
        owner="v0.7 Graphiti exit",
        criteria="Graphiti client construction is deleted and native services own graph access.",
    ),
    GraphitiCompatibilityRecord(
        path="packages/python/sibyl-core/src/sibyl_core/graph/client.py",
        classification="compatibility",
        owner="v0.7 Graphiti exit",
        criteria="Native graph client replaces Graphiti construction and provider adapters.",
    ),
    GraphitiCompatibilityRecord(
        path="packages/python/sibyl-core/src/sibyl_core/graph/entities.py",
        classification="compatibility",
        owner="v0.7 native memory",
        criteria="Native write, exact lookup, semantic search, and entity hydration cover the seeded graph behavior without Graphiti node APIs.",
    ),
    GraphitiCompatibilityRecord(
        path="packages/python/sibyl-core/src/sibyl_core/graph/relationships.py",
        classification="compatibility",
        owner="v0.7 native write adapter",
        criteria="Native relation manager owns relates_to, mentions, and relationship model hydration.",
    ),
    GraphitiCompatibilityRecord(
        path="packages/python/sibyl-core/src/sibyl_core/graph/search_interface.py",
        classification="compatibility",
        owner="v0.7 native retrieval",
        criteria="Compare mode no longer calls Graphiti search and seeded native retrieval is the default path.",
    ),
    GraphitiCompatibilityRecord(
        path="packages/python/sibyl-core/src/sibyl_core/graph/cached_embedder.py",
        classification="compatibility",
        owner="v0.7 native retrieval",
        criteria="Native embedding service owns caching without Graphiti embedder types.",
    ),
    GraphitiCompatibilityRecord(
        path="packages/python/sibyl-core/src/sibyl_core/graph/gemini_embedder.py",
        classification="compatibility",
        owner="v0.7 native retrieval",
        criteria="Native embedding service supports Gemini directly.",
    ),
    GraphitiCompatibilityRecord(
        path="packages/python/sibyl-core/src/sibyl_core/graph/mock_llm.py",
        classification="test",
        owner="v0.7 reflection",
        criteria="Native reflection tests no longer instantiate Graphiti extraction clients.",
    ),
    GraphitiCompatibilityRecord(
        path="packages/python/sibyl-core/src/sibyl_core/tools/admin.py",
        classification="admin",
        owner="v0.7 archive compatibility",
        criteria="Backup restore helpers stop dynamically loading Graphiti node and edge classes.",
    ),
    GraphitiCompatibilityRecord(
        path="packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/*",
        classification="compatibility",
        owner="v0.7 Graphiti exit",
        criteria="No default or fallback memory path constructs Graphiti or calls Graphiti model operation interfaces.",
    ),
)


class SqlUsageVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.session_import_aliases: set[str] = set()
        self.query_import_aliases: set[str] = set()
        self.session_variable_names: set[str] = set()
        self.session_calls: set[str] = set()
        self.query_calls: set[str] = set()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module.startswith(SQL_IMPORT_PREFIXES):
            for alias in node.names:
                local_name = alias.asname or alias.name
                if alias.name in SQL_SESSION_IMPORTS:
                    self.session_import_aliases.add(local_name)
                if alias.name in SQL_QUERY_IMPORTS:
                    self.query_import_aliases.add(local_name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._collect_session_arguments(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._collect_session_arguments(node)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        self._collect_with_session_bindings(node)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._collect_with_session_bindings(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            if node.func.id in self.query_import_aliases:
                self.query_calls.add(node.func.id)
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in SQL_SESSION_CALLS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.session_variable_names
        ):
            self.session_calls.add(node.func.attr)
        self.generic_visit(node)

    def _collect_session_arguments(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        session_type_names = self.session_import_aliases | SQL_SESSION_IMPORTS
        for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            if not arg.annotation:
                continue
            if annotation_names(arg.annotation) & session_type_names:
                self.session_variable_names.add(arg.arg)

    def _collect_with_session_bindings(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            if (
                isinstance(item.optional_vars, ast.Name)
                and "session" in item.optional_vars.id.lower()
            ):
                self.session_variable_names.add(item.optional_vars.id)


def read_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def iter_python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if path.is_file())


def relpath(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def annotation_names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def parse_dependency_name(requirement: str) -> str:
    trimmed = requirement.split(";", maxsplit=1)[0].strip()
    if "[" in trimmed:
        trimmed = trimmed.split("[", maxsplit=1)[0]
    for stop in ("<", ">", "=", "!", "~"):
        if stop in trimmed:
            trimmed = trimmed.split(stop, maxsplit=1)[0]
    return trimmed.strip()


def render_dependency_table(title: str, records: tuple[DependencyRecord, ...]) -> list[str]:
    lines = [f"### {title}"]
    if not records:
        lines.append("- none")
        return lines
    lines.extend(
        [
            "| Project | Scope | Dependency |",
            "| ------- | ----- | ---------- |",
        ]
    )
    for record in records:
        lines.append(f"| `{record.project}` | `{record.scope}` | `{record.dependency}` |")
    return lines


def emit(message: str, stream: TextIO = sys.stdout) -> None:
    stream.write(f"{message}\n")


def collect_rest_surface() -> tuple[
    tuple[str, ...], tuple[HttpRoute, ...], tuple[WebSocketRouteRecord, ...]
]:
    tree = read_ast(APP_PATH)
    rest_routers: list[str] = []
    top_level_routes: list[HttpRoute] = []
    websocket_routes: list[WebSocketRouteRecord] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "app"
                and node.func.attr == "include_router"
                and node.args
                and isinstance(node.args[0], ast.Name)
            ):
                rest_routers.append(node.args[0].id)
            elif isinstance(node.func, ast.Name) and node.func.id == "WebSocketRoute":
                path = (
                    node.args[0].value
                    if node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    else "<dynamic>"
                )
                handler = (
                    node.args[1].id
                    if len(node.args) > 1 and isinstance(node.args[1], ast.Name)
                    else "<dynamic>"
                )
                websocket_routes.append(WebSocketRouteRecord(path=path, handler=handler))

        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "app"
                    and decorator.func.attr in HTTP_METHOD_DECORATORS
                ):
                    path = (
                        decorator.args[0].value
                        if decorator.args
                        and isinstance(decorator.args[0], ast.Constant)
                        and isinstance(decorator.args[0].value, str)
                        else "<dynamic>"
                    )
                    top_level_routes.append(
                        HttpRoute(
                            method=decorator.func.attr.upper(),
                            path=path,
                            handler=node.name,
                        )
                    )

    return (
        tuple(rest_routers),
        tuple(
            sorted(top_level_routes, key=lambda route: (route.method, route.path, route.handler))
        ),
        tuple(sorted(websocket_routes, key=lambda route: (route.path, route.handler))),
    )


def collect_mcp_surface() -> tuple[tuple[McpDecoratorRecord, ...], tuple[McpDecoratorRecord, ...]]:
    tools: list[McpDecoratorRecord] = []
    resources: list[McpDecoratorRecord] = []
    for path in iter_python_files(REPO_ROOT / "apps/api/src"):
        tree = read_ast(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in {"resource", "tool"}
                ):
                    continue
                target: str | None = None
                if (
                    decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                    and isinstance(decorator.args[0].value, str)
                ):
                    target = decorator.args[0].value
                record = McpDecoratorRecord(
                    name=node.name,
                    location=relpath(path),
                    target=target,
                )
                if decorator.func.attr == "tool":
                    tools.append(record)
                else:
                    resources.append(record)
    return (
        tuple(sorted(tools, key=lambda record: (record.location, record.name))),
        tuple(sorted(resources, key=lambda record: (record.location, record.name))),
    )


def collect_sqlmodel_tables() -> tuple[str, ...]:
    if not MODELS_PATH.exists():
        return ()
    tree = read_ast(MODELS_PATH)
    tables: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if any(
            keyword.arg == "table"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        ):
            tables.append(node.name)
    return tuple(tables)


def collect_storage_usage() -> tuple[tuple[SqlUsageRecord, ...], tuple[SqlUsageRecord, ...]]:
    raw_sql_records: list[SqlUsageRecord] = []
    session_only_records: list[SqlUsageRecord] = []
    for root in SOURCE_ROOTS:
        for path in iter_python_files(root):
            visitor = SqlUsageVisitor()
            visitor.visit(read_ast(path))
            if not (
                visitor.session_import_aliases
                or visitor.query_import_aliases
                or visitor.session_calls
                or visitor.query_calls
            ):
                continue
            record = SqlUsageRecord(
                path=relpath(path),
                session_imports=tuple(sorted(visitor.session_import_aliases)),
                query_imports=tuple(sorted(visitor.query_import_aliases)),
                session_calls=tuple(sorted(visitor.session_calls)),
                query_calls=tuple(sorted(visitor.query_calls)),
            )
            if visitor.query_import_aliases or visitor.query_calls:
                raw_sql_records.append(record)
            else:
                session_only_records.append(record)
    return tuple(raw_sql_records), tuple(session_only_records)


def collect_graphiti_imports() -> tuple[GraphitiImportRecord, ...]:
    records: list[GraphitiImportRecord] = []
    for root in SOURCE_ROOTS:
        for path in iter_python_files(root):
            tree = read_ast(path)
            imports: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(GRAPHITI_IMPORT_PREFIXES):
                            imports.add(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module.startswith(GRAPHITI_IMPORT_PREFIXES):
                        imports.add(module)
                elif isinstance(node, ast.Call):
                    dynamic_import = graphiti_dynamic_import_name(node)
                    if dynamic_import is not None:
                        imports.add(dynamic_import)
            if imports:
                records.append(
                    GraphitiImportRecord(path=relpath(path), imports=tuple(sorted(imports)))
                )
    return tuple(records)


def graphiti_dynamic_import_name(node: ast.Call) -> str | None:
    if not node.args:
        return None

    function_name: str | None = None
    if isinstance(node.func, ast.Name):
        function_name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        function_name = node.func.attr

    if function_name not in {"__import__", "import_module"}:
        return None

    module_arg = node.args[0]
    if not isinstance(module_arg, ast.Constant) or not isinstance(module_arg.value, str):
        return None
    if not module_arg.value.startswith(GRAPHITI_IMPORT_PREFIXES):
        return None
    return module_arg.value


def extract_dependency_items(pyproject: dict[str, Any]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    project = pyproject.get("project", {})
    items.extend(("default", dependency) for dependency in project.get("dependencies", []))

    optional_groups = project.get("optional-dependencies", {})
    for group_name, dependencies in optional_groups.items():
        items.extend((f"optional:{group_name}", dependency) for dependency in dependencies)

    dependency_groups = pyproject.get("dependency-groups", {})
    for group_name, dependencies in dependency_groups.items():
        items.extend((f"dependency-group:{group_name}", dependency) for dependency in dependencies)

    return items


def collect_dependencies() -> tuple[DependencyRecord, ...]:
    records: list[DependencyRecord] = []
    seen: set[tuple[str, str, str, str]] = set()
    for pyproject_path in PYPROJECT_PATHS:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project_name = relpath(pyproject_path)
        for scope, requirement in extract_dependency_items(data):
            dependency_name = parse_dependency_name(requirement)
            classification: str | None = None
            if dependency_name in LEGACY_DEPENDENCY_NAMES or "falkordb" in requirement:
                classification = "legacy"
            elif dependency_name in GRAPH_DEPENDENCY_NAMES:
                classification = "graph"
            elif dependency_name in TARGET_DEPENDENCY_NAMES:
                classification = "target"
            if classification is None:
                continue
            key = (project_name, requirement, classification, scope)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                DependencyRecord(
                    project=project_name,
                    dependency=requirement,
                    classification=classification,
                    scope=scope,
                )
            )
    return tuple(
        sorted(
            records,
            key=lambda record: (
                record.classification,
                record.project,
                record.scope,
                record.dependency,
            ),
        )
    )


def collect_runtime_surface() -> RuntimeSurface:
    rest_routers, top_level_http_routes, websocket_routes = collect_rest_surface()
    mcp_tools, mcp_resources = collect_mcp_surface()
    raw_sql_usage, session_storage_usage = collect_storage_usage()
    return RuntimeSurface(
        rest_routers=rest_routers,
        top_level_http_routes=top_level_http_routes,
        websocket_routes=websocket_routes,
        mcp_tools=mcp_tools,
        mcp_resources=mcp_resources,
        sqlmodel_tables=collect_sqlmodel_tables(),
        raw_sql_usage=raw_sql_usage,
        session_storage_usage=session_storage_usage,
        graphiti_imports=collect_graphiti_imports(),
        dependencies=collect_dependencies(),
    )


def _path_matches_allowlist(path: str, allowed: GraphitiCompatibilityRecord) -> bool:
    if allowed.path.endswith("*"):
        return path.startswith(allowed.path.removesuffix("*"))
    return path == allowed.path


def graphiti_allowlist_record(path: str) -> GraphitiCompatibilityRecord | None:
    for allowed in GRAPHITI_COMPATIBILITY_ALLOWLIST:
        if _path_matches_allowlist(path, allowed):
            return allowed
    return None


def graphiti_surface_class(path: str) -> GraphitiSurfaceClass | Literal["default"]:
    allowed = graphiti_allowlist_record(path)
    return "default" if allowed is None else allowed.classification


def _graphiti_path_is_documented(path: str, inventory_text: str) -> bool:
    allowed = graphiti_allowlist_record(path)
    documented_path = path if allowed is None else allowed.path
    return f"`{documented_path}`" in inventory_text


def unclassified_graphiti_imports(
    surface: RuntimeSurface,
    *,
    inventory_path: Path = GRAPHITI_EXIT_INVENTORY_PATH,
) -> tuple[GraphitiImportRecord, ...]:
    if not inventory_path.exists():
        return surface.graphiti_imports
    inventory_text = inventory_path.read_text(encoding="utf-8")
    return tuple(
        record
        for record in surface.graphiti_imports
        if graphiti_allowlist_record(record.path) is None
        or not _graphiti_path_is_documented(record.path, inventory_text)
    )


def default_runtime_graphiti_imports(
    surface: RuntimeSurface,
) -> tuple[GraphitiImportRecord, ...]:
    return tuple(
        record
        for record in surface.graphiti_imports
        if graphiti_allowlist_record(record.path) is None
    )


def render_markdown(surface: RuntimeSurface) -> str:
    legacy_dependencies = tuple(
        record for record in surface.dependencies if record.classification == "legacy"
    )
    graph_dependencies = tuple(
        record for record in surface.dependencies if record.classification == "graph"
    )
    target_dependencies = tuple(
        record for record in surface.dependencies if record.classification == "target"
    )

    lines = [
        "# Runtime Inventory",
        "",
        "Generated from code by `tools/inventory/runtime_surface.py`. Do not hand-edit.",
        "",
        "## Summary",
        f"- REST routers: {len(surface.rest_routers)}",
        f"- Top-level HTTP routes: {len(surface.top_level_http_routes)}",
        f"- WebSocket routes: {len(surface.websocket_routes)}",
        f"- MCP tools: {len(surface.mcp_tools)}",
        f"- MCP resources: {len(surface.mcp_resources)}",
        f"- SQLModel tables: {len(surface.sqlmodel_tables)}",
        f"- Raw SQL query usage files: {len(surface.raw_sql_usage)}",
        f"- Session-backed storage access files: {len(surface.session_storage_usage)}",
        f"- Graphiti import files: {len(surface.graphiti_imports)}",
        f"- Dependency records: {len(surface.dependencies)}",
        "",
        "## API Surface",
        "",
        "### Mounted REST routers",
    ]

    lines.extend(f"- `{router}`" for router in surface.rest_routers)
    lines.extend(
        [
            "",
            "### Top-level HTTP routes",
        ]
    )
    lines.extend(
        f"- `{route.method} {route.path}` → `{route.handler}`"
        for route in surface.top_level_http_routes
    )
    lines.extend(
        [
            "",
            "### WebSocket routes",
        ]
    )
    lines.extend(f"- `{route.path}` → `{route.handler}`" for route in surface.websocket_routes)

    lines.extend(
        [
            "",
            "## MCP Surface",
            "",
            "### Tools",
        ]
    )
    lines.extend(f"- `{record.name}` in `{record.location}`" for record in surface.mcp_tools)
    lines.extend(
        [
            "",
            "### Resources",
        ]
    )
    lines.extend(
        (
            f"- `{record.target}` via `{record.name}` in `{record.location}`"
            if record.target
            else f"- `{record.name}` in `{record.location}`"
        )
        for record in surface.mcp_resources
    )

    lines.extend(
        [
            "",
            "## Storage Coupling",
            "",
            "### SQLModel tables",
        ]
    )
    lines.extend(f"- `{table}`" for table in surface.sqlmodel_tables)
    lines.extend(
        [
            "",
            "### Raw SQL query usage files",
        ]
    )
    lines.extend(
        (
            f"- `{record.path}`"
            f" — session imports: {', '.join(f'`{name}`' for name in record.session_imports) or 'none'}"
            f"; query imports: {', '.join(f'`{name}`' for name in record.query_imports) or 'none'}"
            f"; session calls: {', '.join(f'`{name}`' for name in record.session_calls) or 'none'}"
            f"; query calls: {', '.join(f'`{name}`' for name in record.query_calls) or 'none'}"
        )
        for record in surface.raw_sql_usage
    )

    lines.extend(
        [
            "",
            "### Session-backed storage access files",
        ]
    )
    lines.extend(
        (
            f"- `{record.path}`"
            f" — session imports: {', '.join(f'`{name}`' for name in record.session_imports) or 'none'}"
            f"; query imports: {', '.join(f'`{name}`' for name in record.query_imports) or 'none'}"
            f"; session calls: {', '.join(f'`{name}`' for name in record.session_calls) or 'none'}"
            f"; query calls: {', '.join(f'`{name}`' for name in record.query_calls) or 'none'}"
        )
        for record in surface.session_storage_usage
    )

    lines.extend(
        [
            "",
            "### Graphiti import files",
        ]
    )
    lines.extend(
        (
            f"- `{record.path}` — class: `{graphiti_surface_class(record.path)}`"
            f"; imports: {', '.join(f'`{item}`' for item in record.imports)}"
        )
        for record in surface.graphiti_imports
    )

    lines.extend(["", "## Dependency Inventory", ""])
    lines.extend(render_dependency_table("Legacy and transition dependencies", legacy_dependencies))
    lines.extend([""])
    lines.extend(render_dependency_table("Graph runtime dependencies", graph_dependencies))
    lines.extend([""])
    lines.extend(render_dependency_table("Target SurrealDB dependencies", target_dependencies))
    lines.extend([""])

    return "\n".join(lines)


def check_snapshot(output_path: Path, rendered: str) -> int:
    if not output_path.exists():
        emit(f"Missing snapshot: {relpath(output_path)}", stream=sys.stderr)
        return 1

    existing = output_path.read_text(encoding="utf-8")
    if existing == rendered:
        emit(f"Inventory snapshot is current: {relpath(output_path)}")
        return 0

    diff = difflib.unified_diff(
        existing.splitlines(),
        rendered.splitlines(),
        fromfile=str(relpath(output_path)),
        tofile="generated",
        lineterm="",
    )
    emit("\n".join(diff), stream=sys.stderr)
    return 1


def check_graphiti_exit_inventory(surface: RuntimeSurface) -> int:
    default_imports = default_runtime_graphiti_imports(surface)
    missing = unclassified_graphiti_imports(surface)
    if not default_imports and not missing:
        emit(
            "Graphiti exit inventory covers "
            f"{len(surface.graphiti_imports)} import files with compatibility classes"
        )
        return 0

    if default_imports:
        emit(
            f"Default runtime contains {len(default_imports)} Graphiti import files:",
            stream=sys.stderr,
        )
        for record in default_imports:
            emit(f"- {record.path}", stream=sys.stderr)

    default_import_set = set(default_imports)
    undocumented = tuple(record for record in missing if record not in default_import_set)
    if undocumented:
        emit(
            f"Graphiti exit inventory is missing {len(undocumented)} classified import files:",
            stream=sys.stderr,
        )
        for record in undocumented:
            emit(f"- {record.path}", stream=sys.stderr)
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the Sibyl runtime inventory snapshot.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check the committed snapshot instead of rewriting it.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=SNAPSHOT_PATH,
        help="Output path for the markdown snapshot.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = args.output.resolve()
    surface = collect_runtime_surface()
    rendered = render_markdown(surface)

    if args.check:
        status = check_snapshot(output_path, rendered)
        inventory_status = check_graphiti_exit_inventory(surface)
        return 1 if status or inventory_status else 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    emit(f"Wrote inventory snapshot to {relpath(output_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
