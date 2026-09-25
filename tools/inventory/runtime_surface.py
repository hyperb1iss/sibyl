from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_PATH = REPO_ROOT / "apps/api/src/sibyl/api/app.py"
SCAN_EXCLUDED_PARTS = {
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
# Every Python surface that ships: the server, the core library, the CLI, and the agent hooks.
RUNTIME_IMPORT_ROOTS = (
    REPO_ROOT / "apps/api/src",
    REPO_ROOT / "apps/cli/src",
    REPO_ROOT / "hooks",
    REPO_ROOT / "packages/python/sibyl-core/src",
)
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
GRAPHITI_IMPORT_PREFIXES = ("graphiti", "graphiti" + "_core")
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
# Names compare PEP 503 normalized, so underscore, dotted, and mixed-case spellings match too.
LEGACY_DEPENDENCY_NAMES = {
    "alembic",
    "asyncpg",
    "graphiti",
    "graphiti" + "-core",
    "pgvector",
    "sqlalchemy",
    "sqlmodel",
}
TARGET_DEPENDENCY_NAMES = {"surrealdb"}
# The frozen migration allowlist; shrinking it is progress, growing it needs a reason.
ALLOWED_LEGACY_DEPENDENCIES: frozenset[tuple[str, str]] = frozenset()


def _is_repo_pyproject(path: Path) -> bool:
    relative_parts = path.relative_to(REPO_ROOT).parts
    return not any(part in SCAN_EXCLUDED_PARTS for part in relative_parts)


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
    scope: str


@dataclass(frozen=True, slots=True)
class RuntimeSurface:
    rest_routers: tuple[str, ...]
    top_level_http_routes: tuple[HttpRoute, ...]
    websocket_routes: tuple[WebSocketRouteRecord, ...]
    mcp_tools: tuple[McpDecoratorRecord, ...]
    mcp_resources: tuple[McpDecoratorRecord, ...]
    raw_sql_usage: tuple[SqlUsageRecord, ...]
    session_storage_usage: tuple[SqlUsageRecord, ...]
    graphiti_imports: tuple[GraphitiImportRecord, ...]
    dependencies: tuple[DependencyRecord, ...]


def git_index_paths() -> frozenset[str]:
    git = shutil.which("git")
    if git is None:
        msg = "git executable is required to collect tracked inventory paths"
        raise RuntimeError(msg)
    result = subprocess.run(  # noqa: S603
        [git, "ls-files", "--cached"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    return frozenset(line for line in result.stdout.splitlines() if line)


GIT_INDEX_PATHS = git_index_paths()
PYPROJECT_PATHS = tuple(
    sorted(
        REPO_ROOT / path
        for path in GIT_INDEX_PATHS
        if Path(path).name == "pyproject.toml" and _is_repo_pyproject(REPO_ROOT / path)
    )
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


def iter_runtime_python_files(roots: Sequence[Path]) -> list[Path]:
    return sorted(
        path
        for root in roots
        for path in iter_python_files(root)
        if not SCAN_EXCLUDED_PARTS.intersection(path.relative_to(root).parts)
    )


def relpath(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def annotation_names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


# The PEP 508 name token, which ends before extras, versions, markers, and `@ url` specs.
DEPENDENCY_NAME_PATTERN = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")


def parse_dependency_name(requirement: str) -> str:
    match = DEPENDENCY_NAME_PATTERN.match(requirement)
    return match.group(1) if match else ""


def normalize_dependency_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def classify_dependency(requirement: str) -> str | None:
    name = normalize_dependency_name(parse_dependency_name(requirement))
    if name in LEGACY_DEPENDENCY_NAMES or "falkordb" in requirement.lower():
        return "legacy"
    if name in TARGET_DEPENDENCY_NAMES:
        return "target"
    return None


def matches_module_prefix(module: str, prefixes: Sequence[str]) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)


def emit(message: str, stream: TextIO | None = None) -> None:
    (stream if stream is not None else sys.stdout).write(f"{message}\n")


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


def imported_module_names(tree: ast.AST) -> set[str]:
    """Absolute imports, plus `import_module` and `__import__` calls on a literal name."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.add(node.module)
        elif isinstance(node, ast.Call) and node.args:
            function_name: str | None = None
            if isinstance(node.func, ast.Name):
                function_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                function_name = node.func.attr
            module_arg = node.args[0]
            if (
                function_name in {"__import__", "import_module"}
                and isinstance(module_arg, ast.Constant)
                and isinstance(module_arg.value, str)
            ):
                names.add(module_arg.value)
    return names


def graphiti_imports_in(tree: ast.AST) -> tuple[str, ...]:
    return tuple(
        sorted(
            name
            for name in imported_module_names(tree)
            if matches_module_prefix(name, GRAPHITI_IMPORT_PREFIXES)
        )
    )


def collect_graphiti_imports(
    roots: Sequence[Path] = RUNTIME_IMPORT_ROOTS,
) -> tuple[GraphitiImportRecord, ...]:
    records: list[GraphitiImportRecord] = []
    for path in iter_runtime_python_files(roots):
        imports = graphiti_imports_in(read_ast(path))
        if imports:
            records.append(GraphitiImportRecord(path=relpath(path), imports=imports))
    return tuple(records)


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
            classification = classify_dependency(requirement)
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
        raw_sql_usage=raw_sql_usage,
        session_storage_usage=session_storage_usage,
        graphiti_imports=collect_graphiti_imports(),
        dependencies=collect_dependencies(),
    )


def check_runtime_purity(surface: RuntimeSurface) -> int:
    failed = False

    if surface.raw_sql_usage:
        failed = True
        emit(
            f"Runtime contains {len(surface.raw_sql_usage)} raw SQL query usage files:",
            stream=sys.stderr,
        )
        for record in surface.raw_sql_usage:
            emit(f"- {record.path}", stream=sys.stderr)

    if surface.session_storage_usage:
        failed = True
        emit(
            "Runtime contains "
            f"{len(surface.session_storage_usage)} session-backed storage access files:",
            stream=sys.stderr,
        )
        for record in surface.session_storage_usage:
            emit(f"- {record.path}", stream=sys.stderr)

    if surface.graphiti_imports:
        failed = True
        emit(
            f"Runtime imports Graphiti in {len(surface.graphiti_imports)} files:",
            stream=sys.stderr,
        )
        for record in surface.graphiti_imports:
            emit(f"- {record.path}: {', '.join(record.imports)}", stream=sys.stderr)

    unpinned = tuple(
        record
        for record in surface.dependencies
        if record.classification == "legacy"
        and (record.project, record.dependency) not in ALLOWED_LEGACY_DEPENDENCIES
    )
    if unpinned:
        failed = True
        emit(
            f"Runtime declares {len(unpinned)} legacy dependencies outside the frozen allowlist:",
            stream=sys.stderr,
        )
        for record in unpinned:
            emit(f"- {record.project}: {record.dependency} ({record.scope})", stream=sys.stderr)

    if failed:
        return 1
    emit(
        "Runtime purity holds: no raw SQL, session storage, Graphiti imports, "
        "or unpinned legacy dependencies"
    )
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fail when runtime code carries the legacy stack: raw SQL, session storage, "
            "Graphiti imports, or legacy dependencies."
        )
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    parse_args(argv)
    return check_runtime_purity(collect_runtime_surface())


if __name__ == "__main__":
    raise SystemExit(main())
