from __future__ import annotations

import json
import os
import subprocess
from shutil import which
from typing import NotRequired, TypedDict, cast

from tools.tests.conftest import REPO_ROOT


class MoonTask(TypedDict):
    command: str
    args: NotRequired[list[str]]
    target: str


class MoonTaskQuery(TypedDict):
    tasks: dict[str, dict[str, MoonTask]]


def _root_moon_tasks() -> dict[str, MoonTask]:
    moon = which("moon")
    assert moon is not None

    result = subprocess.run(  # noqa: S603
        [moon, "query", "tasks", "--project", "root"],
        cwd=REPO_ROOT,
        env={**os.environ, "MOON_COLOR": "false"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = cast(MoonTaskQuery, json.loads(result.stdout))
    return payload["tasks"]["root"]


def test_surreal_migration_moon_tasks_match_cli_surface() -> None:
    tasks = _root_moon_tasks()

    expected = {
        "migrate-local-surreal": ("bash", ["tools/dev/migrate-local-surreal.sh"]),
        "migrate-rehearse": ("uv", ["run", "sibyld", "migrate", "rehearse"]),
        "migrate-cutover": ("uv", ["run", "sibyld", "migrate", "cutover"]),
        "auth-flow-replay": ("uv", ["run", "sibyld", "migrate", "auth-flow"]),
        "auth-flow-compare": (
            "uv",
            ["run", "sibyld", "migrate", "auth-flow-compare"],
        ),
        "auth-readonly": ("uv", ["run", "sibyld", "migrate", "auth-readonly"]),
    }

    for task_name, (command, args) in expected.items():
        task = tasks[task_name]
        assert task["target"] == f"root:{task_name}"
        assert task["command"] == command
        assert task["args"] == args
