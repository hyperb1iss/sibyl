"""Execution environment builder for agent subprocess execution.

Standardizes env var injection and sensitive key scrubbing for both
the runner daemon and API worker execution paths.
"""

from __future__ import annotations

import os
from pathlib import Path

# Keys scrubbed from child process environments to prevent credential leakage
SENSITIVE_KEYS = (
    "SIBYL_RUNNER_TOKEN",
    "SIBYL_AUTH_TOKEN",
    "SIBYL_API_KEY",
    "SIBYL_ACCESS_TOKEN",
    "SIBYL_REFRESH_TOKEN",
)


def build_execution_env(
    *,
    task_id: str,
    agent_id: str,
    project_id: str,
    prompt: str,
    worktree_path: str | Path,
    sandbox_id: str | None = None,
    base_env: dict[str, str] | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a scrubbed environment dict for agent subprocess execution.

    Starts from ``base_env`` (defaults to the current process env), strips
    sensitive tokens, then injects standard Sibyl execution variables.

    Args:
        task_id: Sibyl task identifier.
        agent_id: Sibyl agent identifier.
        project_id: Sibyl project identifier.
        prompt: Task prompt injected as ``SIBYL_TASK_PROMPT``.
        worktree_path: Working directory injected as ``SIBYL_WORKTREE_PATH``.
        sandbox_id: Optional sandbox identifier.
        base_env: Starting environment (defaults to ``os.environ``).
        extra: Additional env vars merged last (highest precedence).

    Returns:
        Environment dict safe for subprocess execution.
    """
    env = dict(base_env if base_env is not None else os.environ)

    # Scrub sensitive tokens
    for key in SENSITIVE_KEYS:
        env.pop(key, None)

    # Inject standard Sibyl execution vars
    env["SIBYL_TASK_ID"] = task_id
    env["SIBYL_AGENT_ID"] = agent_id
    env["SIBYL_PROJECT_ID"] = project_id
    env["SIBYL_TASK_PROMPT"] = prompt
    env["SIBYL_WORKTREE_PATH"] = str(worktree_path)

    if sandbox_id:
        env["SIBYL_SANDBOX_ID"] = sandbox_id

    if extra:
        env.update(extra)

    return env
