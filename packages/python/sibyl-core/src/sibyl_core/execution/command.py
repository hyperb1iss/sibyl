"""Execution command resolution for agent subprocess harness.

Resolves the concrete command list used to execute agent tasks,
with support for config overrides, environment variables, and
a safe fallback.
"""

from __future__ import annotations

import os
from typing import Any

# Default fallback: echo the prompt so callers can verify injection works
_DEFAULT_FALLBACK = ["/bin/sh", "-lc", 'printf "%s\\n" "$SIBYL_TASK_PROMPT"']


def resolve_execution_command(
    config: dict[str, Any],
    *,
    fallback_env_var: str = "SIBYL_RUNNER_EXEC_CMD",
    fallback_command: list[str] | None = None,
) -> list[str]:
    """Resolve the subprocess command from config, env, or fallback.

    Resolution order:
    1. ``config["command"]`` / ``config["exec_command"]`` / ``config["runner_command"]``
       — if a list of strings, used directly; if a single string, wrapped in a shell.
    2. Environment variable named by ``fallback_env_var``.
    3. ``fallback_command`` (caller-supplied default).
    4. Built-in echo fallback.

    Args:
        config: Task configuration dict (from task_assign message).
        fallback_env_var: Env var checked when config has no command.
        fallback_command: Explicit fallback command list.

    Returns:
        Command list suitable for ``asyncio.create_subprocess_exec``.
    """
    command = config.get("command") or config.get("exec_command") or config.get("runner_command")

    if isinstance(command, list) and command and all(isinstance(item, str) for item in command):
        return command
    if isinstance(command, str) and command.strip():
        return ["/bin/sh", "-lc", command]

    env_cmd = os.environ.get(fallback_env_var)
    if env_cmd:
        return ["/bin/sh", "-lc", env_cmd]

    return fallback_command if fallback_command is not None else list(_DEFAULT_FALLBACK)
