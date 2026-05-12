from __future__ import annotations

import argparse
import ast
import difflib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "docs/research/rust-port/INVENTORY.md"
GRAPHITI_EXIT_INVENTORY_PATH = REPO_ROOT / "docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md"
APP_PATH = REPO_ROOT / "apps/api/src/sibyl/api/app.py"
MODELS_PATH = REPO_ROOT / "apps/api/src/sibyl/db/models.py"
PYPROJECT_PATHS = [
    REPO_ROOT / "pyproject.toml",
    REPO_ROOT / "apps/api/pyproject.toml",
    REPO_ROOT / "apps/cli/pyproject.toml",
    REPO_ROOT / "packages/python/sibyl-core/pyproject.toml",
]
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
GRAPHITI_EXIT_INVENTORY_GROUPS = ("packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/*",)
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
class DependencyRecord:
    project: str
    dependency: str
    classification: str


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
            "| Project | Dependency |",
            "| ------- | ---------- |",
        ]
    )
    for record in records:
        lines.append(f"| `{record.project}` | `{record.dependency}` |")
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
            if imports:
                records.append(
                    GraphitiImportRecord(path=relpath(path), imports=tuple(sorted(imports)))
                )
    return tuple(records)


def extract_dependency_strings(pyproject: dict[str, Any]) -> list[str]:
    strings: list[str] = []
    project = pyproject.get("project", {})
    strings.extend(project.get("dependencies", []))

    optional_groups = project.get("optional-dependencies", {})
    for dependencies in optional_groups.values():
        strings.extend(dependencies)

    dependency_groups = pyproject.get("dependency-groups", {})
    for dependencies in dependency_groups.values():
        strings.extend(dependencies)

    return strings


def collect_dependencies() -> tuple[DependencyRecord, ...]:
    records: list[DependencyRecord] = []
    for pyproject_path in PYPROJECT_PATHS:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project_name = relpath(pyproject_path)
        for requirement in extract_dependency_strings(data):
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
            records.append(
                DependencyRecord(
                    project=project_name,
                    dependency=requirement,
                    classification=classification,
                )
            )
    return tuple(
        sorted(
            records, key=lambda record: (record.classification, record.project, record.dependency)
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


def _graphiti_path_is_classified(path: str, inventory_text: str) -> bool:
    if f"`{path}`" in inventory_text:
        return True
    for group in GRAPHITI_EXIT_INVENTORY_GROUPS:
        prefix = group.removesuffix("*")
        if path.startswith(prefix) and f"`{group}`" in inventory_text:
            return True
    return False


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
        if not _graphiti_path_is_classified(record.path, inventory_text)
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
        f"- `{record.path}` — {', '.join(f'`{item}`' for item in record.imports)}"
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
    missing = unclassified_graphiti_imports(surface)
    if not missing:
        emit(f"Graphiti exit inventory covers {len(surface.graphiti_imports)} import files")
        return 0

    emit(
        f"Graphiti exit inventory is missing {len(missing)} import files:",
        stream=sys.stderr,
    )
    for record in missing:
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
