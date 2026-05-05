from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TARGETS = (
    REPO_ROOT / "apps/api/src/sibyl/api/routes",
    REPO_ROOT / "apps/api/src/sibyl/auth/dependencies.py",
    REPO_ROOT / "apps/api/src/sibyl/auth/authorization.py",
    REPO_ROOT / "apps/api/src/sibyl/server.py",
    REPO_ROOT / "apps/api/src/sibyl/auth/mcp_auth.py",
    REPO_ROOT / "apps/api/src/sibyl/auth/mcp_oauth.py",
    REPO_ROOT / "packages/python/sibyl-core/src/sibyl_core/services/crawl_sources.py",
    REPO_ROOT / "packages/python/sibyl-core/src/sibyl_core/services/document_search.py",
    REPO_ROOT / "packages/python/sibyl-core/src/sibyl_core/services/link_graph_status.py",
    REPO_ROOT / "packages/python/sibyl-core/src/sibyl_core/tools",
)
FORBIDDEN_MODULE_PREFIXES = {
    "graphiti": "Graphiti runtime import",
    "graphiti_core": "Graphiti runtime import",
    "sibyl.db": "legacy SQL session import",
    "sibyl.db.connection": "legacy SQL session import",
    "sibyl.persistence.legacy": "legacy persistence runtime import",
    "sibyl.auth.api_keys": "legacy auth manager import",
    "sibyl.auth.audit": "legacy auth manager import",
    "sibyl.auth.device_authorization": "legacy auth manager import",
    "sibyl.auth.invitations": "legacy auth manager import",
    "sibyl.auth.memberships": "legacy auth manager import",
    "sibyl.auth.organizations": "legacy auth manager import",
    "sibyl.auth.password_reset": "legacy auth manager import",
    "sibyl.auth.rls": "RLS SQL session wrapper import",
    "sibyl.auth.sessions": "legacy auth manager import",
    "sibyl.auth.users": "legacy auth manager import",
    "sibyl_core.graph": "legacy graph runtime import",
    "sqlalchemy": "raw SQLAlchemy import",
    "sqlmodel": "raw SQLModel import",
}
ALLOWLIST: dict[str, tuple[str, ...]] = {}


@dataclass(frozen=True, slots=True)
class DirectStorageImport:
    path: str
    lineno: int
    module: str
    reason: str
    allowlisted: bool


def display_path(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def iter_python_files(targets: Sequence[Path]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        if target.is_file():
            if target.suffix == ".py":
                files.append(target)
            continue
        if target.is_dir():
            files.extend(path for path in target.rglob("*.py") if path.is_file())
    return sorted(set(files))


def matches_prefix(module: str, prefixes: Sequence[str]) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)


def allowlisted(path: str, module: str, allowlist: Mapping[str, tuple[str, ...]]) -> bool:
    return matches_prefix(module, allowlist.get(path, ()))


def _type_checking_body_ranges(tree: ast.AST) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_type_checking = isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"
        if (
            isinstance(test, ast.Attribute)
            and test.attr == "TYPE_CHECKING"
            and isinstance(test.value, ast.Name)
            and test.value.id == "typing"
        ):
            is_type_checking = True
        if not is_type_checking or not node.body:
            continue
        start = min(getattr(item, "lineno", 0) for item in node.body)
        end = max(getattr(item, "end_lineno", getattr(item, "lineno", 0)) for item in node.body)
        ranges.append((start, end))
    return ranges


def _in_type_checking_block(node: ast.AST, ranges: Sequence[tuple[int, int]]) -> bool:
    lineno = getattr(node, "lineno", 0)
    return any(start <= lineno <= end for start, end in ranges)


def collect_direct_storage_imports(
    *,
    targets: Sequence[Path] | None = None,
    allowlist: Mapping[str, tuple[str, ...]] | None = None,
) -> list[DirectStorageImport]:
    selected_targets = tuple(targets or DEFAULT_TARGETS)
    active_allowlist = allowlist or ALLOWLIST
    violations: list[DirectStorageImport] = []

    for path in iter_python_files(selected_targets):
        module_path = display_path(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        type_checking_ranges = _type_checking_body_ranges(tree)
        for node in ast.walk(tree):
            if _in_type_checking_block(node, type_checking_ranges):
                continue
            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)
            else:
                continue

            for imported_module in imported_modules:
                for prefix, reason in FORBIDDEN_MODULE_PREFIXES.items():
                    if not matches_prefix(imported_module, (prefix,)):
                        continue
                    violations.append(
                        DirectStorageImport(
                            path=module_path,
                            lineno=getattr(node, "lineno", 0),
                            module=imported_module,
                            reason=reason,
                            allowlisted=allowlisted(module_path, imported_module, active_allowlist),
                        )
                    )
                    break

    return sorted(
        violations, key=lambda violation: (violation.path, violation.lineno, violation.module)
    )


def render_report(violations: Sequence[DirectStorageImport]) -> str:
    unallowlisted = [violation for violation in violations if not violation.allowlisted]
    allowlisted_violations = [violation for violation in violations if violation.allowlisted]

    lines = [
        (
            "direct storage access guard: "
            f"{len(unallowlisted)} unallowlisted, {len(allowlisted_violations)} allowlisted"
        )
    ]

    if unallowlisted:
        lines.append("")
        lines.append("unallowlisted imports:")
        for violation in unallowlisted:
            lines.append(
                f"  - {violation.path}:{violation.lineno} imports `{violation.module}`"
                f" ({violation.reason})"
            )

    if allowlisted_violations:
        lines.append("")
        lines.append("allowlisted debt:")
        for violation in allowlisted_violations:
            lines.append(f"  - {violation.path}:{violation.lineno} imports `{violation.module}`")

    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail when routes or MCP surfaces import legacy storage runtimes directly."
    )
    parser.add_argument(
        "--path",
        action="append",
        dest="paths",
        help="Optional file or directory to scan. Defaults to the route and MCP surfaces.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    targets = tuple(Path(path).resolve() for path in args.paths) if args.paths else DEFAULT_TARGETS
    violations = collect_direct_storage_imports(targets=targets)
    stdout.write(f"{render_report(violations)}\n")
    return 1 if any(not violation.allowlisted for violation in violations) else 0


if __name__ == "__main__":
    raise SystemExit(main())
