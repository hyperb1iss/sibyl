"""Every deployment surface runs the one SurrealDB release Sibyl supports.

Code imports the release from ``sibyl_core.backends.surreal.server_version``.
Compose files, the Helm chart, the Ansible role, CI and the deployment docs
cannot, so they carry literal image tags, store paths and storage settings;
these tests hold each of them to that module, so a server bump or a storage
setting change has to reach every surface in the same commit.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from sibyl_core.backends.surreal.server_version import (
    SURREAL_SERVER_IMAGE,
    SURREAL_SERVER_VERSION,
    SURREAL_STORAGE_ENV,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# (compose file, SurrealDB service) for every compose service that starts a server.
COMPOSE_SERVERS = (
    ("docker-compose.yml", "surrealdb"),
    ("docker-compose.yml", "surrealdb-eval"),
    ("docker-compose.prod.yml", "surrealdb"),
    ("docker-compose.quickstart.yml", "surrealdb"),
    ("compose.e2e.yml", "surrealdb-e2e"),
    (".devcontainer/docker-compose.yml", "surrealdb"),
    ("infra/ansible/roles/sibyl/files/docker-compose.yml", "surrealdb"),
)

# Where a literal SurrealDB image tag may appear. Tests are left out: they pin
# older tags on purpose to exercise upgrades. Architecture notes are dated
# records of what ran then.
PIN_PATHSPECS = (
    "*.yml",
    "*.yaml",
    "*.md",
    "*.sh",
    "*.tpl",
    "*.j2",
    ":!:**/tests/**",
    ":!:docs/architecture/**",
    ":!:benchmarks/**",
)

IMAGE_LITERAL = re.compile(r"surrealdb/surrealdb:v?(\d+\.\d+\.\d+[^\s\"'`}]*)")


def _yaml(relative: str) -> Any:
    return yaml.safe_load((REPO_ROOT / relative).read_text(encoding="utf-8"))


def _service(relative: str, name: str) -> dict[str, Any]:
    return _yaml(relative)["services"][name]


def _environment(service: dict[str, Any]) -> dict[str, str | None]:
    """A service's environment as a mapping; a bare list entry passes the host value."""
    environment = service.get("environment") or {}
    if isinstance(environment, dict):
        return {key: None if value is None else str(value) for key, value in environment.items()}
    mapped: dict[str, str | None] = {}
    for entry in environment:
        key, sep, value = str(entry).partition("=")
        mapped[key] = value if sep else None
    return mapped


def _store_path(service: dict[str, Any]) -> str:
    command = service["command"]
    if isinstance(command, str):
        command = command.split()
    return str(command[-1])


@pytest.mark.parametrize(("relative", "name"), COMPOSE_SERVERS)
def test_compose_servers_run_the_supported_image(relative: str, name: str) -> None:
    image = _service(relative, name)["image"]
    assert image in {SURREAL_SERVER_IMAGE, f"${{SIBYL_SURREAL_IMAGE:-{SURREAL_SERVER_IMAGE}}}"}


@pytest.mark.parametrize(("relative", "name"), COMPOSE_SERVERS)
def test_compose_servers_open_surrealkv(relative: str, name: str) -> None:
    path = _store_path(_service(relative, name))
    if path.startswith("${SIBYL_SURREAL_PATH"):
        default = path.removeprefix("${SIBYL_SURREAL_PATH").removesuffix("}")
        if default.startswith(":?"):
            # The Ansible role renders the path; its default is checked below.
            return
        path = default.removeprefix(":-")
    assert path.startswith("surrealkv:///data/"), path


@pytest.mark.parametrize(("relative", "name"), COMPOSE_SERVERS)
def test_compose_servers_carry_the_storage_settings(relative: str, name: str) -> None:
    environment = _environment(_service(relative, name))
    for key, value in SURREAL_STORAGE_ENV.items():
        assert environment.get(key) == value, f"{relative} {name} {key}"
    # 4.x ignores every RocksDB setting with a warning.
    assert not [key for key in environment if key.startswith("SURREAL_ROCKSDB_")]


def test_helm_chart_runs_the_supported_image_on_surrealkv() -> None:
    chart = _yaml("charts/surrealdb/Chart.yaml")
    values = _yaml("charts/surrealdb/values.yaml")["surrealdb"]

    assert chart["appVersion"] == SURREAL_SERVER_VERSION
    assert values["image"]["tag"] == f"v{SURREAL_SERVER_VERSION}"
    assert values["surrealdb"]["path"].startswith("surrealkv:")
    environment = {entry["name"]: entry.get("value") for entry in values["podExtraEnv"]}
    for key, value in SURREAL_STORAGE_ENV.items():
        assert environment.get(key) == value


def test_ansible_role_runs_the_supported_image_on_surrealkv() -> None:
    defaults = _yaml("infra/ansible/roles/sibyl/defaults/main.yml")

    assert defaults["sibyl_surreal_image"] == SURREAL_SERVER_IMAGE
    assert defaults["sibyl_surreal_path"] == "surrealkv:///data/sibyl.db"
    assert defaults["sibyl_surreal_upgrade_from_3x"] is False


def test_every_literal_surrealdb_image_names_the_supported_release() -> None:
    git = shutil.which("git")
    assert git is not None
    files = subprocess.run(  # noqa: S603 - fixed git arguments
        [git, "ls-files", "--", *PIN_PATHSPECS],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.split()
    stale = [
        f"{relative}: {match.group(0)}"
        for relative in files
        for match in IMAGE_LITERAL.finditer((REPO_ROOT / relative).read_text(encoding="utf-8"))
        if match.group(1) != SURREAL_SERVER_VERSION
    ]
    assert files
    assert stale == []


def test_no_server_start_path_opens_rocksdb() -> None:
    """4.x has no RocksDB engine; `rocksdb://` survives only as a deprecated alias."""
    paths = [
        ".github/actions/start-surrealdb/action.yml",
        ".github/workflows/eval.yml",
        ".github/workflows/longmemeval-v2.yml",
        "charts/surrealdb/values.yaml",
        *dict.fromkeys(relative for relative, _ in COMPOSE_SERVERS),
    ]
    offenders = [
        f"{relative}: {line.strip()}"
        for relative in paths
        for line in (REPO_ROOT / relative).read_text(encoding="utf-8").splitlines()
        if re.search(r"rocksdb:(//)?/", line) and not line.lstrip().startswith("#")
    ]
    assert offenders == []
