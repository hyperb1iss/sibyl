"""CLI configuration store using TOML.

Manages ~/.sibyl/config.toml for CLI-specific settings.
Server settings stay in .env - this is just for the CLI client.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found]

import tomli_w

# Default configuration
DEFAULT_CONFIG: dict[str, Any] = {
    "server": {
        "url": "http://localhost:3334",
    },
    "defaults": {
        "project": "",
    },
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
