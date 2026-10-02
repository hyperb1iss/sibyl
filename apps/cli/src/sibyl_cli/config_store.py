"""CLI configuration store using TOML.

Manages ~/.sibyl/config.toml for CLI-specific settings.
Server settings come from process env or explicit deployment env files.

Supports multiple named contexts, each with its own server URL,
organization, and default project settings.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import tomli_w


class ConfigStoreError(Exception):
    """Base class for configuration failures safe to show at the CLI boundary."""


class ConfigCorruptionError(ConfigStoreError):
    """The on-disk configuration is malformed or has invalid known fields."""

    def __init__(self, path: Path, cause: Exception, *, problem: str = "invalid TOML") -> None:
        self.path = path
        self.cause = cause
        super().__init__(
            f"Config file {path} has {problem}: {cause}. "
            "Repair it or run 'sibyl config reset' to replace it."
        )


# =============================================================================
# Context Model
# =============================================================================


@dataclass
class Context:
    """A named CLI context bundling server, org, and project settings.

    Contexts allow working with multiple Sibyl instances (e.g., local, staging, prod)
    without reconfiguring between sessions.
    """

    name: str
    server_url: str = "http://localhost:3334"
    org_slug: str | None = None  # None = auto-pick first/only org
    default_project: str | None = None
    insecure: bool = False  # Skip SSL verification (for self-signed certs)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for TOML storage."""
        return {
            "server_url": self.server_url,
            "org_slug": self.org_slug or "",
            "default_project": self.default_project or "",
            "insecure": self.insecure,
        }

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> Context:
        """Create from TOML dict."""
        return cls(
            name=name,
            server_url=data.get("server_url", "http://localhost:3334"),
            org_slug=data.get("org_slug") or None,
            default_project=data.get("default_project") or None,
            insecure=bool(data.get("insecure", False)),
        )


# =============================================================================
# Default Configuration
# =============================================================================

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {
        "url": "http://localhost:3334",
    },
    "defaults": {
        "project": "",
    },
    "paths": {},  # path -> project_id mappings
    "active_context": "",  # Name of active context (empty = use legacy server.url)
    "contexts": {},  # name -> {server_url, org_slug, default_project}
}


class _Unset:
    pass


_UNSET = _Unset()


def config_dir() -> Path:
    """Get the Sibyl config directory (~/.sibyl)."""
    return Path.home() / ".sibyl"


def config_path() -> Path:
    """Get the config file path (~/.sibyl/config.toml)."""
    return config_dir() / "config.toml"


def config_exists() -> bool:
    """Check if config file exists."""
    return config_path().exists()


def ensure_config_dir() -> Path:
    """Ensure the config directory exists."""
    path = config_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _config_file_lock(path: Path | None = None) -> Iterator[None]:
    """Serialize read-modify-write operations through a sidecar lock file."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f"{target.name}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        if locked and os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        elif locked:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load_config_unlocked(path: Path) -> dict[str, Any]:
    config = _deep_copy(DEFAULT_CONFIG)
    if not path.exists():
        return config

    try:
        with path.open("rb") as file:
            file_config = tomllib.load(file)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigCorruptionError(path, exc) from exc

    _deep_merge(config, file_config)
    _validate_config_schema(config, path)
    return config


def _schema_failure(path: Path, field: str, expected: str, value: Any) -> None:
    actual = type(value).__name__
    cause = ValueError(f"{field} must be {expected}, found {actual}")
    raise ConfigCorruptionError(path, cause, problem="an invalid schema") from cause


def _validate_config_schema(config: dict[str, Any], path: Path) -> None:
    """Validate every known config container before it can steer a command."""
    server = config.get("server")
    if not isinstance(server, dict):
        _schema_failure(path, "server", "a table", server)
    server_url = server.get("url")
    if not isinstance(server_url, str) or not server_url:
        _schema_failure(path, "server.url", "a non-empty string", server_url)

    defaults = config.get("defaults")
    if not isinstance(defaults, dict):
        _schema_failure(path, "defaults", "a table", defaults)
    default_project = defaults.get("project")
    if not isinstance(default_project, str):
        _schema_failure(path, "defaults.project", "a string", default_project)

    active_context = config.get("active_context")
    if not isinstance(active_context, str):
        _schema_failure(path, "active_context", "a string", active_context)

    paths = config.get("paths")
    if not isinstance(paths, dict):
        _schema_failure(path, "paths", "a table", paths)
    for mapped_path, entry in paths.items():
        if isinstance(entry, str):
            continue
        if not isinstance(entry, dict):
            _schema_failure(path, f"paths.{mapped_path}", "a string or table", entry)
        for field in ("project", "context"):
            value = entry.get(field)
            if value is not None and not isinstance(value, str):
                _schema_failure(path, f"paths.{mapped_path}.{field}", "a string", value)

    contexts = config.get("contexts")
    if not isinstance(contexts, dict):
        _schema_failure(path, "contexts", "a table", contexts)
    for name, context in contexts.items():
        if not isinstance(context, dict):
            _schema_failure(path, f"contexts.{name}", "a table", context)
        context_url = context.get("server_url")
        if not isinstance(context_url, str) or not context_url:
            _schema_failure(
                path,
                f"contexts.{name}.server_url",
                "a non-empty string",
                context_url,
            )
        for field in ("org_slug", "default_project"):
            value = context.get(field)
            if value is not None and not isinstance(value, str):
                _schema_failure(path, f"contexts.{name}.{field}", "a string", value)
        insecure = context.get("insecure")
        if insecure is not None and not isinstance(insecure, bool):
            _schema_failure(path, f"contexts.{name}.insecure", "a boolean", insecure)


def load_config() -> dict[str, Any]:
    """Load config from TOML file.

    Returns default config merged with file contents.
    Missing keys get default values.
    """
    return _load_config_unlocked(config_path())


def _write_config_unlocked(path: Path, config: dict[str, Any]) -> None:
    """Durably replace a config file without exposing partial TOML."""
    path.parent.mkdir(parents=True, exist_ok=True)
    validated = _deep_copy(DEFAULT_CONFIG)
    _deep_merge(validated, config)
    _validate_config_schema(validated, path)
    content = tomli_w.dumps(config).encode("utf-8")
    fd: int | None = None
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as file:
            fd = None
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary)


def save_config(config: dict[str, Any]) -> None:
    """Atomically replace the complete config under the mutation lock."""
    path = config_path()
    with _config_file_lock(path):
        _write_config_unlocked(path, config)


def update_config[Result](mutation: Callable[[dict[str, Any]], Result]) -> Result:
    """Apply one locked read-modify-write transaction to the CLI config."""
    path = config_path()
    with _config_file_lock(path):
        config = _load_config_unlocked(path)
        result = mutation(config)
        _write_config_unlocked(path, config)
    return result


def ensure_config_file() -> bool:
    """Create the default config once under lock, preserving a concurrent writer."""
    path = config_path()
    with _config_file_lock(path):
        if path.exists():
            _load_config_unlocked(path)
            return False
        _write_config_unlocked(path, _deep_copy(DEFAULT_CONFIG))
        return True


def get(key: str, default: Any = None) -> Any:
    """Get a config value by dot-notation key.

    Examples:
        get("server.url") -> "http://localhost:3334"
        get("defaults.project") -> ""
    """
    config = load_config()
    return _get_nested(config, key, default)


def set_value(key: str, value: Any) -> None:
    """Set a config value by dot-notation key.

    Examples:
        set_value("server.url", "http://example.com:3334")
        set_value("defaults.project", "my-project")
    """

    def mutation(config: dict[str, Any]) -> None:
        _set_nested(config, key, value)

    update_config(mutation)


def get_server_url() -> str:
    """Get the server URL from config."""
    return str(get("server.url", DEFAULT_CONFIG["server"]["url"]))


def get_default_project() -> str:
    """Get the default project from config."""
    return str(get("defaults.project", ""))


def reset_config() -> None:
    """Reset config to defaults."""
    save_config(_deep_copy(DEFAULT_CONFIG))


# --- Path mapping for project context ---


def _path_entry_fields(value: Any) -> tuple[str | None, str | None]:
    """Unpack a ``[paths]`` value into (project_id, context_name).

    A path link is stored either as a bare string (legacy: project only) or as a
    table ``{project, context}``. Either field may be absent, e.g. a directory tree
    pinned to a context with no project yet.
    """
    if isinstance(value, str):
        return (value or None, None)
    if isinstance(value, dict):
        project = value.get("project") or None
        context = value.get("context") or None
        return (project, context)
    return (None, None)


def _make_path_entry(project_id: str | None, context: str | None) -> str | dict[str, str]:
    """Build a ``[paths]`` value, preferring the legacy bare-string form.

    TOML cannot hold null, so absent fields are simply omitted. A project-only link
    stays a bare string to avoid churning existing configs; anything carrying a
    context is promoted to a table.
    """
    if context:
        entry: dict[str, str] = {}
        if project_id:
            entry["project"] = project_id
        entry["context"] = context
        return entry
    return project_id or ""


def get_path_mappings() -> dict[str, str]:
    """Get all path -> project_id mappings (context-only links omitted)."""
    config = load_config()
    result: dict[str, str] = {}
    for mapped_path, value in config.get("paths", {}).items():
        project, _ = _path_entry_fields(value)
        if project:
            result[mapped_path] = project
    return result


def get_path_context_mappings() -> dict[str, str]:
    """Get all path -> context_name mappings (project-only links omitted)."""
    config = load_config()
    result: dict[str, str] = {}
    for mapped_path, value in config.get("paths", {}).items():
        _, context = _path_entry_fields(value)
        if context:
            result[mapped_path] = context
    return result


def get_path_link(path: str) -> tuple[str | None, str | None]:
    """Get (project_id, context_name) pinned for an exact normalized path."""
    normalized = str(Path(path).expanduser().resolve())
    config = load_config()
    return _path_entry_fields(config.get("paths", {}).get(normalized))


def set_path_mapping(path: str, project_id: str, *, context: str | _Unset | None = _UNSET) -> None:
    """Pin a directory to a project (and optionally the context it lives on).

    Args:
        path: Directory path (will be normalized, ~ expanded)
        project_id: Project ID to associate with this path
        context: Context name the project lives on. ``_UNSET`` keeps any existing
            context pin; ``None`` clears it; a name pins project + context together.
    """
    normalized = str(Path(path).expanduser().resolve())

    def mutation(config: dict[str, Any]) -> None:
        paths = config.setdefault("paths", {})
        _, existing_context = _path_entry_fields(paths.get(normalized))
        new_context = existing_context if isinstance(context, _Unset) else context
        paths[normalized] = _make_path_entry(project_id, new_context)

    update_config(mutation)


def set_path_context(path: str, context: str) -> None:
    """Pin a directory tree to a context, preserving any existing project link."""
    normalized = str(Path(path).expanduser().resolve())

    def mutation(config: dict[str, Any]) -> None:
        paths = config.setdefault("paths", {})
        existing_project, _ = _path_entry_fields(paths.get(normalized))
        paths[normalized] = _make_path_entry(existing_project, context)

    update_config(mutation)


def remove_path_context(path: str) -> bool:
    """Clear the context pin for a path, keeping any project link.

    Returns True if a context pin was removed, False if there was none.
    """
    normalized = str(Path(path).expanduser().resolve())

    def mutation(config: dict[str, Any]) -> bool:
        paths = config.setdefault("paths", {})
        existing_project, existing_context = _path_entry_fields(paths.get(normalized))
        if not existing_context:
            return False
        paths[normalized] = _make_path_entry(existing_project, None)
        return True

    return update_config(mutation)


def remove_path_mapping(path: str) -> bool:
    """Remove a path link entirely (both project and context pins).

    Returns:
        True if a link was removed, False if not found
    """
    normalized = str(Path(path).expanduser().resolve())

    def mutation(config: dict[str, Any]) -> bool:
        paths = config.setdefault("paths", {})
        if normalized not in paths:
            return False
        del paths[normalized]
        return True

    return update_config(mutation)


@dataclass(frozen=True)
class LinkCleanup:
    """One finding of a ``[paths]`` cleanup plan."""

    # drop_* and lift change the table; keep_* are reported and left alone.
    action: Literal[
        "drop_empty", "drop_missing", "drop_redundant", "lift", "keep_differs", "keep_conflict"
    ]
    path: str
    project: str | None = None
    context: str | None = None
    # For worktree entries: the equivalent path in the main repository, and the
    # link the repository holds (or will hold after a lift).
    target: str | None = None
    kept_project: str | None = None
    kept_context: str | None = None

    @property
    def changes(self) -> bool:
        return not self.action.startswith("keep_")


def _within_live_checkout(path: Path) -> bool:
    """Whether a missing path still sits inside an existing git checkout.

    A link on a directory that exists only on some branch (stored at the
    repository's equivalent path by ``project link`` in a worktree) is missing
    in the main checkout but still governs that worktree.
    """
    current = path
    while not current.exists():
        if current == current.parent:
            return False
        current = current.parent
    while current != current.parent:
        if (current / ".git").exists():
            return True
        current = current.parent
    return False


def _redundant(entry: tuple[str | None, str | None], repo: tuple[str | None, str | None]) -> bool:
    """A worktree entry adds nothing when every field it sets matches the repository's."""
    return all(
        value is None or value == repo_value for value, repo_value in zip(entry, repo, strict=True)
    )


def _plan_cleanup(paths: dict[str, Any]) -> list[LinkCleanup]:
    entries = {mapped: _path_entry_fields(value) for mapped, value in paths.items()}
    actions: list[LinkCleanup] = []
    dead: set[str] = set()
    for mapped in sorted(entries):
        project, context = entries[mapped]
        if not project and not context:
            actions.append(LinkCleanup("drop_empty", mapped))
            dead.add(mapped)
        elif not Path(mapped).exists() and not _within_live_checkout(Path(mapped)):
            actions.append(LinkCleanup("drop_missing", mapped, project, context))
            dead.add(mapped)

    by_target: dict[str, list[str]] = {}
    for mapped in sorted(entries):
        if mapped in dead:
            continue
        target, worktree_root = canonical_link_path(mapped)
        if worktree_root is not None:
            by_target.setdefault(target, []).append(mapped)

    for target, members in sorted(by_target.items()):
        repo = entries.get(target) if target not in dead else None
        if repo and any(repo):
            for mapped in members:
                action = "drop_redundant" if _redundant(entries[mapped], repo) else "keep_differs"
                actions.append(
                    LinkCleanup(
                        action,
                        mapped,
                        *entries[mapped],
                        target=target,
                        kept_project=repo[0],
                        kept_context=repo[1],
                    )
                )
        elif len({entries[mapped] for mapped in members}) == 1:
            first, *rest = members
            actions.append(LinkCleanup("lift", first, *entries[first], target=target))
            for mapped in rest:
                actions.append(
                    LinkCleanup(
                        "drop_redundant",
                        mapped,
                        *entries[mapped],
                        target=target,
                        kept_project=entries[first][0],
                        kept_context=entries[first][1],
                    )
                )
        else:
            for mapped in members:
                actions.append(
                    LinkCleanup("keep_conflict", mapped, *entries[mapped], target=target)
                )
    return actions


def plan_link_cleanup() -> list[LinkCleanup]:
    """Plan a ``[paths]`` cleanup that leaves every live directory routing as before.

    Dropped: empty entries, entries whose whole checkout is gone (removed
    worktrees), and worktree entries the repository's link already implies.
    Lifted: a worktree link onto its repository when the repository has none
    and every worktree of it agrees. Kept and reported: worktree pins that
    differ from the repository or from each other, since dropping them would
    change where that worktree routes.
    """
    return _plan_cleanup(load_config().get("paths", {}))


class LinkCleanupConflictError(RuntimeError):
    """The links changed between planning and applying a cleanup."""


def apply_link_cleanup(planned: list[LinkCleanup]) -> None:
    """Apply a cleanup plan, re-planning under the config lock first.

    Another session may have changed the links since the plan was shown; a
    stale plan could then overwrite a fresh link, so it is refused instead.
    """

    def mutation(config: dict[str, Any]) -> None:
        paths = config.setdefault("paths", {})
        if _plan_cleanup(paths) != planned:
            raise LinkCleanupConflictError(
                "Directory links changed since the plan was made; run the prune again"
            )
        for action in planned:
            if action.action == "lift" and action.target:
                paths[action.target] = _make_path_entry(action.project, action.context)
            if action.changes:
                paths.pop(action.path, None)

    update_config(mutation)


def _gitdir_from_file(git_file: Path) -> Path | None:
    """The directory a ``.git`` file points at, resolved against that file's directory.

    Git writes relative gitdirs for submodules and for worktrees created with
    ``--relative-paths``; resolving them against the process cwd misreads both.
    """
    try:
        content = git_file.read_text().strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    return (git_file.parent / content[len("gitdir:") :].strip()).resolve()


def _linked_worktree_main_repo(gitdir: Path) -> Path | None:
    """Main checkout of a linked worktree's private gitdir, else None.

    A linked worktree's gitdir is ``<common dir>/worktrees/<name>``, with a
    ``commondir`` file naming the common dir. A submodule's gitdir lives under
    ``modules/`` instead, even when the submodule sits inside a worktree.
    """
    if gitdir.parent.name != "worktrees":
        return None
    common_dir = gitdir.parent.parent
    commondir_file = gitdir / "commondir"
    if commondir_file.is_file():
        try:
            common_dir = (gitdir / commondir_file.read_text().strip()).resolve()
        except OSError:
            return None
    if common_dir.name != ".git" or common_dir != gitdir.parent.parent:
        return None
    return common_dir.parent


def _worktree_location(start_path: Path) -> tuple[Path, Path] | None:
    """Locate the git worktree containing a path: (worktree root, main repo root).

    Returns None inside a main checkout and outside any repository. A
    submodule's ``.git`` file is stepped over, so a path in a submodule of a
    worktree still belongs to that worktree.
    """
    current = start_path
    while current != current.parent:
        git_path = current / ".git"
        if git_path.is_dir():
            return None
        if git_path.is_file():
            gitdir = _gitdir_from_file(git_path)
            main_repo = _linked_worktree_main_repo(gitdir) if gitdir else None
            if main_repo is not None:
                return current, main_repo
        current = current.parent
    return None


def _resolve_worktree_main_repo(start_path: Path) -> Path | None:
    """Main repository root for a path inside a git worktree, else None."""
    location = _worktree_location(start_path)
    return location[1] if location else None


def canonical_link_path(path: str) -> tuple[str, str | None]:
    """Where a directory link for ``path`` belongs: (path to store, worktree root).

    A link made inside a git worktree is stored at the equivalent path in the
    main repository, so every worktree of that repository (current and future)
    routes the same way. Worktrees are disposable; a pin on one goes stale when
    it is removed and silently overrides the repository while it exists.
    """
    resolved = Path(path).expanduser().resolve()
    location = _worktree_location(resolved)
    if location is None:
        return str(resolved), None
    worktree_root, main_repo = location
    return str(main_repo / resolved.relative_to(worktree_root)), str(worktree_root)


def _best_prefix(
    search_path: Path, mappings: dict[str, str], *, within: Path | None = None
) -> tuple[str | None, str | None]:
    """Longest mapped ancestor of ``search_path``: (value, mapped path).

    ``within`` restricts candidates to mapped paths inside that directory.
    """
    best: tuple[str | None, str | None] = (None, None)
    best_length = -1
    for mapped_path, value in mappings.items():
        mapped = Path(mapped_path)
        if within is not None and not mapped.is_relative_to(within):
            continue
        if search_path.is_relative_to(mapped) and len(mapped.parts) > best_length:
            best = (value, mapped_path)
            best_length = len(mapped.parts)
    return best


def _match_link(cwd: Path, mappings: dict[str, str]) -> tuple[str | None, str | None]:
    """Resolve one link field for a directory: (value, mapped path).

    Outside a worktree the longest mapped ancestor wins. Inside one, precedence
    is by meaning rather than by raw path length (a worktree path is always
    longer than its repository's): an explicit pin inside the worktree, then the
    equivalent path in the main repository (so a link on a subdirectory of the
    repository applies in every worktree too), then the nearest ancestor
    directory pin of either location.
    """
    location = _worktree_location(cwd)
    if location is None:
        return _best_prefix(cwd, mappings)
    worktree_root, main_repo = location
    pinned = _best_prefix(cwd, mappings, within=worktree_root)
    if pinned[0]:
        return pinned
    equivalent = main_repo / cwd.relative_to(worktree_root)
    inherited = _best_prefix(equivalent, mappings, within=main_repo)
    if inherited[0]:
        return inherited
    around_worktree = _best_prefix(cwd, mappings)
    around_repo = _best_prefix(main_repo, mappings)
    candidates = [hit for hit in (around_worktree, around_repo) if hit[0] and hit[1]]
    if not candidates:
        return None, None
    return max(candidates, key=lambda hit: len(Path(str(hit[1])).parts))


def resolve_project_from_cwd() -> str | None:
    """Resolve project ID from current working directory.

    Walks up from cwd looking for the nearest linked directory. Inside a git
    worktree, the main repository's links apply too (see ``_match_link``).

    Returns:
        Project ID if found, None otherwise
    """
    import os

    mappings = get_path_mappings()
    if not mappings:
        return None
    return _match_link(Path(os.getcwd()).resolve(), mappings)[0]


def resolve_context_from_cwd() -> str | None:
    """Resolve the pinned context name from the current working directory.

    Walks up from cwd looking for the nearest context pin, and (when inside a
    git worktree) the main repository's pins too. This is what lets a directory
    route to its own Sibyl server without a manual ``context use``. The context
    resolves independently of the project, so a worktree link that names only a
    project still inherits its repository's server.

    Returns:
        Context name if a pin covers the cwd, None otherwise.
    """
    import os

    mappings = get_path_context_mappings()
    if not mappings:
        return None
    return _match_link(Path(os.getcwd()).resolve(), mappings)[0]


def get_current_context() -> tuple[str | None, str | None]:
    """Get current project context.

    If in a git worktree, also checks the main repo's path.

    Returns:
        Tuple of (project_id, matched_path) or (None, None) if no context
    """
    import os

    mappings = get_path_mappings()
    if not mappings:
        return None, None
    return _match_link(Path(os.getcwd()).resolve(), mappings)


# --- Private helpers ---


def _deep_copy(d: dict[str, Any]) -> dict[str, Any]:
    """Deep copy a nested dict."""
    result: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            result[k] = _deep_copy(v)
        else:
            result[k] = v
    return result


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """Deep merge override into base (mutates base)."""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _get_nested(d: dict[str, Any], key: str, default: Any = None) -> Any:
    """Get nested value by dot-notation key."""
    keys = key.split(".")
    current: Any = d
    for k in keys:
        if isinstance(current, dict) and k in current:
            current = current[k]
        else:
            return default
    return current


def _set_nested(d: dict[str, Any], key: str, value: Any) -> None:
    """Set nested value by dot-notation key (mutates d)."""
    keys = key.split(".")
    current = d
    for k in keys[:-1]:
        if k not in current or not isinstance(current[k], dict):
            current[k] = {}
        current = current[k]
    current[keys[-1]] = value


# =============================================================================
# Context Management
# =============================================================================


def get_active_context_name() -> str | None:
    """Get the name of the active context.

    Returns:
        Context name, or None if no active context (legacy mode).
    """
    name = get("active_context", "")
    return name if name else None


def set_active_context(name: str | None) -> None:
    """Set the active context by name.

    Args:
        name: Context name, or None to clear (use legacy mode).
    """
    set_value("active_context", name or "")


def get_context(name: str) -> Context | None:
    """Get a context by name.

    Args:
        name: Context name.

    Returns:
        Context if found, None otherwise.
    """
    config = load_config()
    contexts = config.get("contexts", {})
    if name in contexts:
        return Context.from_dict(name, contexts[name])
    return None


def get_active_context() -> Context | None:
    """Get the currently active context.

    Returns:
        Active Context, or None if no context is active (legacy mode).
    """
    name = get_active_context_name()
    if not name:
        return None
    return get_context(name)


def list_contexts() -> list[Context]:
    """List all configured contexts.

    Returns:
        List of all contexts.
    """
    config = load_config()
    contexts = config.get("contexts", {})
    return [Context.from_dict(name, data) for name, data in contexts.items()]


def create_context(
    name: str,
    server_url: str,
    org_slug: str | None = None,
    default_project: str | None = None,
    *,
    set_active: bool = False,
    insecure: bool = False,
) -> Context:
    """Create a new context.

    Args:
        name: Context name (e.g., "prod", "local").
        server_url: Server URL for this context.
        org_slug: Organization slug (optional, auto-picked if None).
        default_project: Default project ID (optional).
        set_active: If True, make this the active context.
        insecure: If True, skip SSL verification (for self-signed certs).

    Returns:
        The created Context.

    Raises:
        ValueError: If context with this name already exists.
    """
    context = Context(
        name=name,
        server_url=server_url,
        org_slug=org_slug,
        default_project=default_project,
        insecure=insecure,
    )

    def mutation(config: dict[str, Any]) -> Context:
        contexts = config.setdefault("contexts", {})
        if name in contexts:
            raise ValueError(f"Context '{name}' already exists")
        contexts[name] = context.to_dict()
        if set_active:
            config["active_context"] = name
        return context

    return update_config(mutation)


def update_context(
    name: str,
    server_url: str | None = None,
    org_slug: str | _Unset | None = _UNSET,
    default_project: str | _Unset | None = _UNSET,
    insecure: bool | None = None,
) -> Context:
    """Update an existing context.

    Args:
        name: Context name to update.
        server_url: New server URL (None = keep existing).
        org_slug: New org slug (_UNSET = keep existing, None = clear).
        default_project: New default project (_UNSET = keep existing, None = clear).
        insecure: SSL verification setting (None = keep existing).

    Returns:
        The updated Context.

    Raises:
        ValueError: If context doesn't exist.
    """

    def mutation(config: dict[str, Any]) -> Context:
        contexts = config.setdefault("contexts", {})
        if name not in contexts:
            raise ValueError(f"Context '{name}' not found")
        ctx_data = contexts[name]
        if server_url is not None:
            ctx_data["server_url"] = server_url
        if not isinstance(org_slug, _Unset):
            ctx_data["org_slug"] = org_slug or ""
        if not isinstance(default_project, _Unset):
            ctx_data["default_project"] = default_project or ""
        if insecure is not None:
            ctx_data["insecure"] = insecure
        return Context.from_dict(name, ctx_data)

    return update_config(mutation)


def delete_context(name: str) -> bool:
    """Delete a context.

    Args:
        name: Context name to delete.

    Returns:
        True if deleted, False if not found.
    """

    def mutation(config: dict[str, Any]) -> bool:
        contexts = config.setdefault("contexts", {})
        if name not in contexts:
            return False
        del contexts[name]
        if config.get("active_context") == name:
            config["active_context"] = ""
        return True

    return update_config(mutation)


class UnknownContextError(Exception):
    """An explicitly selected context does not exist.

    Selecting a context by name is a statement about which server to touch, so
    a name that resolves to nothing must stop the command. Falling back to the
    active context would silently retarget the write at whatever server happens
    to be active, which is how a typo in `-C staging` reaches production.
    """

    def __init__(self, name: str, source: str = "--context") -> None:
        self.name = name
        self.source = source
        self.known = [ctx.name for ctx in list_contexts()]
        known = ", ".join(self.known) if self.known else "none configured"
        super().__init__(f"Unknown context '{name}' (from {source}). Known contexts: {known}.")


def require_known_context(name: str | None, source: str = "--context") -> None:
    """Reject an explicitly named context that does not exist."""
    if name and get_context(name) is None:
        raise UnknownContextError(name, source)


def explicit_context_selection() -> tuple[str, str] | None:
    """Return the explicitly selected context and where the selection came from."""
    from sibyl_cli.state import get_context_override

    if override := get_context_override():
        return override, _override_source()
    if pinned := resolve_context_from_cwd():
        return pinned, "directory pin"
    return None


def _override_source() -> str:
    """Name the surface the override came from.

    Typer fills the --context parameter from SIBYL_CONTEXT, so the stored
    override cannot tell them apart; argv can.
    """
    import os
    import sys

    for arg in sys.argv[1:]:
        if arg in {"-C", "--context"} or arg.startswith("--context="):
            return "--context"
    if os.environ.get("SIBYL_CONTEXT", "").strip():
        return "SIBYL_CONTEXT"
    return "--context"


def resolve_context_name() -> str | None:
    """Resolve the effective context name across all selection inputs.

    Priority:
    1. ``--context`` flag / ``SIBYL_CONTEXT`` env (explicit, per-invocation)
    2. Directory pin (``resolve_context_from_cwd``)
    3. Active context from config

    Returns None in legacy mode (no context configured or selected).
    """
    from sibyl_cli.state import context_selection_ignored, get_context_override

    if context_selection_ignored():
        return get_active_context_name()

    override = get_context_override()
    if override:
        return override

    pinned = resolve_context_from_cwd()
    if pinned:
        return pinned

    return get_active_context_name()


def resolve_effective_context() -> Context | None:
    """Resolve the effective :class:`Context` (see :func:`resolve_context_name`)."""
    name = resolve_context_name()
    if not name:
        return None
    return get_context(name)


def get_effective_server_url() -> str:
    """Get the effective server URL.

    Delegates to the single resolver in ``client`` and strips the ``/api``
    suffix, so callers that want a display URL cannot drift from the URL
    requests are actually sent to. Resolving here independently is what left
    this function blind to SIBYL_API_URL.

    Returns:
        Server URL to use.
    """
    from sibyl_cli.client import resolve_api_base_url

    api_url = resolve_api_base_url(resolve_context_name())
    return api_url.rstrip("/").removesuffix("/api") or api_url


def get_effective_project() -> str | None:
    """Get the effective default project, considering context and path.

    Priority:
    1. Path mapping for cwd
    2. Effective context's default_project
    3. Legacy defaults.project

    Returns:
        Project ID or None.
    """
    # First check path mapping
    project = resolve_project_from_cwd()
    if project:
        return project

    # Then check the effective context
    context = resolve_effective_context()
    if context and context.default_project:
        return context.default_project

    # Finally legacy default
    default = get_default_project()
    return default if default else None
