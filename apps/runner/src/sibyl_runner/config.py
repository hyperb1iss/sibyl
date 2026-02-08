"""Configuration management for Sibyl Runner."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class RunnerConfig:
    """Runner daemon configuration."""

    # Connection
    server_url: str = ""
    runner_id: str = ""
    graph_runner_id: str = ""

    # Identity
    name: str = ""

    # Capabilities
    max_concurrent_agents: int = 3
    capabilities: list[str] = field(default_factory=lambda: ["docker"])

    # Authentication
    access_token: str = ""
    refresh_token: str = ""
    runner_token: str = ""

    # Runtime mode
    sandbox_mode: bool = False
    sandbox_id: str = ""

    # Paths
    worktree_base: Path = field(default_factory=lambda: Path.home() / ".sibyl" / "worktrees")
    log_dir: Path = field(default_factory=lambda: Path.home() / ".sibyl" / "logs")

    # Reconnection
    reconnect_interval: float = 5.0
    max_reconnect_attempts: int = 10
    heartbeat_interval: float = 30.0


def get_default_config_path() -> Path:
    """Get default config file path."""
    if os.environ.get("SIBYL_CONFIG_FILE"):
        return Path(os.environ["SIBYL_CONFIG_FILE"])
    if os.environ.get("SIBYL_CONFIG_DIR"):
        return Path(os.environ["SIBYL_CONFIG_DIR"]) / "runner.yaml"
    return Path.home() / ".config" / "sibyl" / "runner.yaml"


def _parse_bool(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _parse_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _apply_env_overrides(config: RunnerConfig, file_fields: set[str]) -> RunnerConfig:
    env = os.environ

    if env.get("SIBYL_SERVER_URL"):
        config.server_url = env["SIBYL_SERVER_URL"]
    if env.get("SIBYL_RUNNER_ID"):
        config.runner_id = env["SIBYL_RUNNER_ID"]
    if env.get("SIBYL_GRAPH_RUNNER_ID"):
        config.graph_runner_id = env["SIBYL_GRAPH_RUNNER_ID"]
    if env.get("SIBYL_RUNNER_NAME"):
        config.name = env["SIBYL_RUNNER_NAME"]
    if env.get("SIBYL_RUNNER_TOKEN"):
        config.runner_token = env["SIBYL_RUNNER_TOKEN"]
    if env.get("SIBYL_ACCESS_TOKEN"):
        config.access_token = env["SIBYL_ACCESS_TOKEN"]
    if env.get("SIBYL_SANDBOX_ID"):
        config.sandbox_id = env["SIBYL_SANDBOX_ID"]

    if env.get("SIBYL_MAX_AGENTS"):
        parsed = _parse_int(env["SIBYL_MAX_AGENTS"])
        if parsed is not None and parsed > 0:
            config.max_concurrent_agents = parsed

    sandbox_mode_from_flag: bool | None = None
    if env.get("SIBYL_SANDBOX_MODE"):
        sandbox_mode_from_flag = _parse_bool(env["SIBYL_SANDBOX_MODE"])

    if sandbox_mode_from_flag is None and env.get("SIBYL_RUNNER_MODE"):
        mode = env["SIBYL_RUNNER_MODE"].strip().lower()
        if mode == "sandbox":
            sandbox_mode_from_flag = True
        elif mode in {"registered", "default"}:
            sandbox_mode_from_flag = False

    if sandbox_mode_from_flag is not None:
        config.sandbox_mode = sandbox_mode_from_flag
    elif env.get("SIBYL_SANDBOX_ID") or env.get("SIBYL_RUNNER_TOKEN"):
        # If sandbox-specific identifiers are present, default into sandbox mode.
        config.sandbox_mode = True

    explicit_worktree = (
        "worktree_base" in file_fields
        or "SIBYL_WORKTREE_BASE" in env
        or "SIBYL_SANDBOX_WORKTREE_BASE" in env
    )
    worktree_override = env.get("SIBYL_SANDBOX_WORKTREE_BASE") or env.get("SIBYL_WORKTREE_BASE")
    if worktree_override:
        config.worktree_base = Path(worktree_override)
    elif config.sandbox_mode and not explicit_worktree:
        sandbox_key = config.sandbox_id or config.runner_id or "default"
        config.worktree_base = Path("/tmp/sibyl/sandboxes") / sandbox_key / "worktrees"

    if env.get("SIBYL_LOG_DIR"):
        config.log_dir = Path(env["SIBYL_LOG_DIR"])

    return config


def load_config(config_file: Path | None = None) -> RunnerConfig:
    """Load configuration from file.

    Args:
        config_file: Path to config file. If None, uses default.

    Returns:
        Loaded configuration, or empty config if file doesn't exist.
    """
    path = config_file or get_default_config_path()

    data: dict = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    # Convert path strings to Path objects
    if "worktree_base" in data:
        data["worktree_base"] = Path(data["worktree_base"])
    if "log_dir" in data:
        data["log_dir"] = Path(data["log_dir"])

    config = RunnerConfig(**data)
    return _apply_env_overrides(config, set(data.keys()))


def save_config(config: RunnerConfig, config_file: Path | None = None) -> None:
    """Save configuration to file.

    Args:
        config: Configuration to save.
        config_file: Path to config file. If None, uses default.
    """
    path = config_file or get_default_config_path()

    # Ensure directory exists
    path.parent.mkdir(parents=True, exist_ok=True)

    # Convert to dict, converting Path objects to strings
    data = {
        "server_url": config.server_url,
        "runner_id": config.runner_id,
        "graph_runner_id": config.graph_runner_id,
        "name": config.name,
        "max_concurrent_agents": config.max_concurrent_agents,
        "capabilities": config.capabilities,
        "sandbox_mode": config.sandbox_mode,
        "sandbox_id": config.sandbox_id,
        "worktree_base": str(config.worktree_base),
        "log_dir": str(config.log_dir),
        "reconnect_interval": config.reconnect_interval,
        "max_reconnect_attempts": config.max_reconnect_attempts,
        "heartbeat_interval": config.heartbeat_interval,
    }

    # Don't save tokens to file for security
    # (they should be obtained via login flow)

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False)
