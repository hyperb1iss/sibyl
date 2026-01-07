"""CLI configuration store using TOML.

Manages ~/.sibyl/config.toml for CLI-specific settings.
Server settings stay in .env - this is just for the CLI client.

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
    import tomli as tomllib  # type: ignore[import-not-found]

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


def set_server_url(url: str) -> None:
    """Set the server URL in config."""
    set_value("server.url", url)


def get_default_project() -> str:
    """Get the default project from config."""
    return str(get("defaults.project", ""))


def set_default_project(project: str) -> None:
    """Set the default project in config."""
    set_value("defaults.project", project)


def reset_config() -> None:
    """Reset config to defaults."""
    save_config(_deep_copy(DEFAULT_CONFIG))


# --- Path mapping for project context ---


def get_path_mappings() -> dict[str, str]:
    """Get all path -> project_id mappings."""
    config = load_config()
    return config.get("paths", {})


def set_path_mapping(path: str, project_id: str) -> None:
    """Set a path -> project_id mapping.

    Args:
        path: Directory path (will be normalized, ~ expanded)
        project_id: Project ID to associate with this path
    """
    # Normalize path: expand ~ and resolve to absolute
    normalized = str(Path(path).expanduser().resolve())

    config = load_config()
    if "paths" not in config:
        config["paths"] = {}
    config["paths"][normalized] = project_id
    save_config(config)


def remove_path_mapping(path: str) -> bool:
    """Remove a path mapping.

    Args:
        path: Directory path to unlink

    Returns:
        True if mapping was removed, False if not found
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
    org_slug: str | None = ...,  # type: ignore[assignment]
    default_project: str | None = ...,  # type: ignore[assignment]
    insecure: bool | None = None,
) -> Context:
    """Update an existing context.

    Args:
        name: Context name to update.
        server_url: New server URL (None = keep existing).
        org_slug: New org slug (... = keep existing, None = clear).
        default_project: New default project (... = keep existing, None = clear).
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
    if org_slug is not ...:
        ctx_data["org_slug"] = org_slug or ""
    if default_project is not ...:
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


def get_effective_server_url() -> str:
    """Get the effective server URL, considering active context.

    Priority:
    1. Active context's server_url
    2. Legacy server.url config
    3. Default localhost

    Returns:
        Server URL to use.
    """
    context = get_active_context()
    if context:
        return context.server_url
    return get_server_url()


def get_effective_project() -> str | None:
    """Get the effective default project, considering context and path.

    Priority:
    1. Path mapping for cwd
    2. Active context's default_project
    3. Legacy defaults.project

    Returns:
        Project ID or None.
    """
    # First check path mapping
    project = resolve_project_from_cwd()
    if project:
        return project

    # Then check active context
    context = get_active_context()
    if context and context.default_project:
        return context.default_project

    # Finally legacy default
    default = get_default_project()
    return default if default else None
