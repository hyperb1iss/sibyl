"""Configuration management for Sibyl Runner."""

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

    # Paths
    worktree_base: Path = field(default_factory=lambda: Path.home() / ".sibyl" / "worktrees")
    log_dir: Path = field(default_factory=lambda: Path.home() / ".sibyl" / "logs")

    # Reconnection
    reconnect_interval: float = 5.0
    max_reconnect_attempts: int = 10
    heartbeat_interval: float = 30.0


def get_default_config_path() -> Path:
    """Get default config file path."""
    return Path.home() / ".config" / "sibyl" / "runner.yaml"


def load_config(config_file: Path | None = None) -> RunnerConfig:
    """Load configuration from file.

    Args:
        config_file: Path to config file. If None, uses default.

    Returns:
        Loaded configuration, or empty config if file doesn't exist.
    """
    path = config_file or get_default_config_path()

    if not path.exists():
        return RunnerConfig()

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    # Convert path strings to Path objects
    if "worktree_base" in data:
        data["worktree_base"] = Path(data["worktree_base"])
    if "log_dir" in data:
        data["log_dir"] = Path(data["log_dir"])

    return RunnerConfig(**data)


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
        "worktree_base": str(config.worktree_base),
        "log_dir": str(config.log_dir),
        "reconnect_interval": config.reconnect_interval,
        "max_reconnect_attempts": config.max_reconnect_attempts,
        "heartbeat_interval": config.heartbeat_interval,
    }

    # Don't save tokens to file for security
    # (they should be obtained via login flow)

    with open(path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False)
