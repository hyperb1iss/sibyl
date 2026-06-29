"""CLI configuration store using TOML.

Manages ~/.sibyl/config.toml for CLI-specific settings.
Server settings come from process env or explicit deployment env files.

Supports multiple named contexts, each with its own server URL,
organization, and default project settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib

import tomli_w

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


def load_config() -> dict[str, Any]:
    """Load config from TOML file.

    Returns default config merged with file contents.
    Missing keys get default values.
    """
    config = _deep_copy(DEFAULT_CONFIG)

    path = config_path()
    if path.exists():
        try:
            with open(path, "rb") as f:
                file_config = tomllib.load(f)
            _deep_merge(config, file_config)
        except (OSError, tomllib.TOMLDecodeError):
            # If file is missing/corrupted, return defaults
            pass

    return config


def save_config(config: dict[str, Any]) -> None:
    """Save config to TOML file."""
    ensure_config_dir()
    path = config_path()

    with open(path, "wb") as f:
        tomli_w.dump(config, f)


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
    config = load_config()
    _set_nested(config, key, value)
    save_config(config)


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


def _set_path_entry(path: str, project_id: str | None, context: str | None) -> None:
    normalized = str(Path(path).expanduser().resolve())
    config = load_config()
    paths = config.setdefault("paths", {})
    if not project_id and not context:
        paths.pop(normalized, None)
    else:
        paths[normalized] = _make_path_entry(project_id, context)
    save_config(config)


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


def set_path_mapping(path: str, project_id: str, *, context: str | None | _Unset = _UNSET) -> None:
    """Pin a directory to a project (and optionally the context it lives on).

    Args:
        path: Directory path (will be normalized, ~ expanded)
        project_id: Project ID to associate with this path
        context: Context name the project lives on. ``_UNSET`` keeps any existing
            context pin; ``None`` clears it; a name pins project + context together.
    """
    _, existing_context = get_path_link(path)
    new_context = existing_context if isinstance(context, _Unset) else context
    _set_path_entry(path, project_id, new_context)


def set_path_context(path: str, context: str) -> None:
    """Pin a directory tree to a context, preserving any existing project link."""
    existing_project, _ = get_path_link(path)
    _set_path_entry(path, existing_project, context)


def remove_path_context(path: str) -> bool:
    """Clear the context pin for a path, keeping any project link.

    Returns True if a context pin was removed, False if there was none.
    """
    existing_project, existing_context = get_path_link(path)
    if not existing_context:
        return False
    _set_path_entry(path, existing_project, None)
    return True


def remove_path_mapping(path: str) -> bool:
    """Remove a path link entirely (both project and context pins).

    Returns:
        True if a link was removed, False if not found
    """
    normalized = str(Path(path).expanduser().resolve())

    config = load_config()
    paths = config.get("paths", {})
    if normalized in paths:
        del paths[normalized]
        save_config(config)
        return True
    return False


def _resolve_worktree_main_repo(start_path: Path) -> Path | None:
    """Detect if path is inside a git worktree and resolve to main repo.

    Git worktrees have a .git file (not directory) containing:
        gitdir: /path/to/main/repo/.git/worktrees/<worktree-name>

    Args:
        start_path: Path to check (typically cwd)

    Returns:
        Main repo path if in a worktree, None otherwise
    """
    # Walk up to find .git file/directory
    current = start_path
    while current != current.parent:
        git_path = current / ".git"
        if git_path.exists():
            if git_path.is_file():
                # Worktree detected - parse gitdir
                try:
                    content = git_path.read_text().strip()
                    if content.startswith("gitdir:"):
                        gitdir = content[7:].strip()
                        # gitdir looks like: /main/repo/.git/worktrees/branch-name
                        # Walk up from gitdir to find the main .git, then its parent
                        gitdir_path = Path(gitdir).resolve()
                        # Should be under .git/worktrees/, go up to .git then to repo root
                        if "worktrees" in gitdir_path.parts:
                            # Find the .git directory (parent of worktrees)
                            worktrees_idx = gitdir_path.parts.index("worktrees")
                            main_git = Path(*gitdir_path.parts[:worktrees_idx])
                            if main_git.name == ".git":
                                return main_git.parent
                except (OSError, ValueError):
                    pass
            # Regular repo or failed to parse - stop searching
            return None
        current = current.parent
    return None


def _find_project_in_mappings(
    search_path: Path, mappings: dict[str, str]
) -> tuple[str | None, int]:
    """Find best matching project for a path in mappings.

    Returns:
        Tuple of (project_id, match_length) or (None, 0) if not found
    """
    best_match: str | None = None
    best_length = 0

    for mapped_path, project_id in mappings.items():
        mapped = Path(mapped_path)
        try:
            search_path.relative_to(mapped)
            if len(mapped_path) > best_length:
                best_match = project_id
                best_length = len(mapped_path)
        except ValueError:
            continue

    return best_match, best_length


def resolve_project_from_cwd() -> str | None:
    """Resolve project ID from current working directory.

    Walks up from cwd looking for longest matching path prefix.
    If in a git worktree, also checks the main repo's path.

    Returns:
        Project ID if found, None otherwise
    """
    import os

    cwd = Path(os.getcwd()).resolve()
    mappings = get_path_mappings()

    if not mappings:
        return None

    # First try direct cwd match
    best_match, best_length = _find_project_in_mappings(cwd, mappings)

    # If in a worktree, also check the main repo path
    main_repo = _resolve_worktree_main_repo(cwd)
    if main_repo:
        repo_match, repo_length = _find_project_in_mappings(main_repo, mappings)
        # Use main repo match if it's better (or only match)
        if repo_match and repo_length > best_length:
            best_match = repo_match

    return best_match


def resolve_context_from_cwd() -> str | None:
    """Resolve the pinned context name from the current working directory.

    Walks up from cwd looking for the longest matching path prefix in the context
    pins, and (when inside a git worktree) the main repo path too. This is what lets
    a directory route to its own Sibyl server without a manual ``context use``.

    Returns:
        Context name if a pin covers the cwd, None otherwise.
    """
    import os

    cwd = Path(os.getcwd()).resolve()
    mappings = get_path_context_mappings()

    if not mappings:
        return None

    best_match, best_length = _find_project_in_mappings(cwd, mappings)

    main_repo = _resolve_worktree_main_repo(cwd)
    if main_repo:
        repo_match, repo_length = _find_project_in_mappings(main_repo, mappings)
        if repo_match and repo_length > best_length:
            best_match = repo_match

    return best_match


def _find_project_with_path(
    search_path: Path, mappings: dict[str, str]
) -> tuple[str | None, str | None, int]:
    """Find best matching project for a path, returning both ID and matched path.

    Returns:
        Tuple of (project_id, matched_path, match_length)
    """
    best_match: str | None = None
    best_path: str | None = None
    best_length = 0

    for mapped_path, project_id in mappings.items():
        mapped = Path(mapped_path)
        try:
            search_path.relative_to(mapped)
            if len(mapped_path) > best_length:
                best_match = project_id
                best_path = mapped_path
                best_length = len(mapped_path)
        except ValueError:
            continue

    return best_match, best_path, best_length


def get_current_context() -> tuple[str | None, str | None]:
    """Get current project context.

    If in a git worktree, also checks the main repo's path.

    Returns:
        Tuple of (project_id, matched_path) or (None, None) if no context
    """
    import os

    cwd = Path(os.getcwd()).resolve()
    mappings = get_path_mappings()

    if not mappings:
        return None, None

    # First try direct cwd match
    best_match, best_path, best_length = _find_project_with_path(cwd, mappings)

    # If in a worktree, also check the main repo path
    main_repo = _resolve_worktree_main_repo(cwd)
    if main_repo:
        repo_match, repo_path, repo_length = _find_project_with_path(main_repo, mappings)
        # Use main repo match if it's better (or only match)
        if repo_match and repo_length > best_length:
            best_match = repo_match
            best_path = repo_path

    return best_match, best_path


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
    config = load_config()
    contexts = config.get("contexts", {})

    if name in contexts:
        raise ValueError(f"Context '{name}' already exists")

    context = Context(
        name=name,
        server_url=server_url,
        org_slug=org_slug,
        default_project=default_project,
        insecure=insecure,
    )

    contexts[name] = context.to_dict()
    config["contexts"] = contexts
    save_config(config)

    if set_active:
        set_active_context(name)

    return context


def update_context(
    name: str,
    server_url: str | None = None,
    org_slug: str | None | _Unset = _UNSET,
    default_project: str | None | _Unset = _UNSET,
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
    config = load_config()
    contexts = config.get("contexts", {})

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

    config["contexts"] = contexts
    save_config(config)

    return Context.from_dict(name, ctx_data)


def delete_context(name: str) -> bool:
    """Delete a context.

    Args:
        name: Context name to delete.

    Returns:
        True if deleted, False if not found.
    """
    config = load_config()
    contexts = config.get("contexts", {})

    if name not in contexts:
        return False

    del contexts[name]
    config["contexts"] = contexts

    # Clear active context if it was the deleted one
    if config.get("active_context") == name:
        config["active_context"] = ""

    save_config(config)
    return True


def resolve_context_name() -> str | None:
    """Resolve the effective context name across all selection inputs.

    Priority:
    1. ``--context`` flag / ``SIBYL_CONTEXT`` env (explicit, per-invocation)
    2. Directory pin (``resolve_context_from_cwd``)
    3. Active context from config

    Returns None in legacy mode (no context configured or selected).
    """
    from sibyl_cli.state import get_context_override

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

    Priority:
    1. Effective context's server_url (override > directory pin > active)
    2. Legacy server.url config
    3. Default localhost

    Returns:
        Server URL to use.
    """
    context = resolve_effective_context()
    if context:
        return context.server_url
    return get_server_url()


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
