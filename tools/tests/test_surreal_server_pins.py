"""Every deployment surface runs the one SurrealDB release Sibyl supports.

Code imports the release from ``sibyl_core.backends.surreal.server_version``.
Compose files, the Helm chart, the Ansible role, CI and the deployment docs
cannot, so they carry literal image tags, store paths and storage settings;
these tests hold each of them to that module, so a server bump or a storage
setting change has to reach every surface in the same commit.
"""

from __future__ import annotations

import os
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
    SURREAL_STORE_GUARD,
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

# The literal pins the scan finds today; fewer means a pin moved out of its reach.
KNOWN_IMAGE_LITERALS = 21
# The chart starts two SurrealDB servers: the deployment's and the restore drill's.
CHART_SURREAL_SERVERS = 2
# The role reads the major with three regex_replace steps.
ANSIBLE_MAJOR_STEPS = 3

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
    matches = [
        (relative, match)
        for relative in files
        for match in IMAGE_LITERAL.finditer((REPO_ROOT / relative).read_text(encoding="utf-8"))
    ]
    stale = [
        f"{relative}: {match.group(0)}"
        for relative, match in matches
        if match.group(1) != SURREAL_SERVER_VERSION
    ]
    assert len(matches) >= KNOWN_IMAGE_LITERALS
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
    # The store guard names rocksdb:// to refuse it, not to open it.
    guard_lines = {line.strip() for line in SURREAL_STORE_GUARD.replace("$", "$$").splitlines()}
    offenders = [
        f"{relative}: {line.strip()}"
        for relative in paths
        for line in (REPO_ROOT / relative).read_text(encoding="utf-8").splitlines()
        if re.search(r"rocksdb:(//)?/", line)
        and not line.lstrip().startswith("#")
        and line.strip() not in guard_lines
    ]
    assert offenders == []


def _store_path(service: dict[str, Any]) -> str:
    command = service["command"]
    return str(command[-1] if isinstance(command, list) else command.split()[-1])


@pytest.mark.parametrize(("relative", "name"), COMPOSE_SERVERS)
def test_compose_servers_start_behind_the_store_guard(relative: str, name: str) -> None:
    """The one-shot ahead of each server refuses an image and path that misread the data."""
    services = _yaml(relative)["services"]
    server = services[name]
    inits = [dep for dep in (server.get("depends_on") or {}) if dep.endswith("init")]
    if not inits:
        # Only a server whose image no setting can change may skip the guard.
        assert "${" not in server["image"], f"{relative} {name} has no guarded init"
        return
    (init,) = inits
    script = services[init]["command"][-1]
    assert script.startswith(SURREAL_STORE_GUARD.replace("$", "$$")), f"{relative} {init}"
    environment = _environment(services[init])
    assert environment["SURREAL_IMAGE"] == server["image"]
    assert environment["SURREAL_STORE"] == _store_path(server)


def test_helm_every_surrealdb_server_carries_the_storage_settings() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm CLI is not installed")
    rendered = subprocess.run(  # noqa: S603 - fixed helm arguments
        [
            helm,
            "template",
            "pins",
            "charts/surrealdb",
            "--set",
            "export.enabled=true",
            "--set",
            "restoreDrill.enabled=true",
            "--set",
            "snapshot.enabled=true",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    servers = []
    for document in yaml.safe_load_all(rendered):
        if not document:
            continue
        spec = document.get("spec", {})
        pod = (
            spec.get("template", {}).get("spec")
            or spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec")
            or {}
        )
        for container in [*pod.get("initContainers", []), *pod.get("containers", [])]:
            if "surrealdb/surrealdb" in container.get("image", ""):
                servers.append(container)
    assert len(servers) == CHART_SURREAL_SERVERS
    for container in servers:
        environment = {entry["name"]: entry.get("value") for entry in container.get("env", [])}
        for key, value in SURREAL_STORAGE_ENV.items():
            assert environment.get(key) == value, container["name"]


def _ansible_major(image: str) -> str:
    """The role's own regex_replace chain, applied with Python's re as Ansible does."""
    tasks = (REPO_ROOT / "infra/ansible/roles/sibyl/tasks/main.yml").read_text(encoding="utf-8")
    expression = tasks.split("sibyl_surreal_major: >-", 1)[1].split("}}", 1)[0]
    steps = re.findall(r"regex_replace\('([^']*)', '([^']*)'\)", expression)
    assert len(steps) == ANSIBLE_MAJOR_STEPS
    for pattern, replacement in steps:
        image = re.sub(pattern, replacement, image)
    return image


@pytest.mark.parametrize(
    ("image", "major"),
    [
        ("surrealdb/surrealdb:v4.0.0", "4"),
        ("surrealdb/surrealdb:v3.2.4", "3"),
        ("docker.io/surrealdb/surrealdb:v3.2.4", "3"),
        ("registry.example:5000/mirror/surrealdb:3.2.4", "3"),
        ("docker.io/surrealdb/surrealdb:v4.0.0@sha256:0123abcd", "4"),
        ("surrealdb/surrealdb@sha256:0123abcd", None),
        ("surrealdb/surrealdb:latest", None),
        ("registry.example:5000/surrealdb", None),
    ],
)
def test_ansible_reads_the_major_from_any_image_reference(image: str, major: str | None) -> None:
    found = _ansible_major(image)
    if major is None:
        # The role refuses a reference whose result is not a bare number.
        assert not found.isdigit(), found
    else:
        assert found == major


_HELD_MAJOR = 3
_SUPPORTED_MAJOR = 4
# Image references by the SurrealDB major they name; None means no version at all.
_GUARD_IMAGES = {
    "surrealdb/surrealdb:v3.2.4": 3,
    "docker.io/surrealdb/surrealdb:3.2.4@sha256:ab": 3,
    "surrealdb/surrealdb:v4.0.0": 4,
    "registry.example:5000/mirror/surrealdb:v4.0.0": 4,
    "surrealdb/surrealdb:latest": None,
    "surrealdb/surrealdb@sha256:ab": None,
    "registry.example:5000/surrealdb": None,
}
_GUARD_STORES = {
    "rocksdb": "rocksdb://{store}",
    "surrealkv": "surrealkv://{store}",
    "relative surrealkv": "surrealkv://data/sibyl.db",
    "relative rocksdb": "rocksdb://data/sibyl.db",
}
_GUARD_LAYOUTS = (
    "empty",
    "missing",
    "rocksdb",
    "surrealkv",
    "converted",
    "backup only",
    "mixed",
    "unreadable rocksdb",
    "rocksdb under an unreadable parent",
)
_UNREADABLE = {"unreadable rocksdb", "rocksdb under an unreadable parent"}


def _lay_out(root: Path, layout: str) -> Path:
    data = root / "data"
    data.mkdir()
    store = data / "sibyl.db"
    if layout != "missing":
        store.mkdir()
    if layout in {"rocksdb", "mixed", *_UNREADABLE}:
        (store / "CURRENT").touch()
    if layout in {"surrealkv", "converted", "mixed"}:
        (store / "manifest").mkdir()
    if layout in {"converted", "backup only"}:
        (store / "_rocksdb_backup").mkdir()
    if layout == "unreadable rocksdb":
        store.chmod(0)
    if layout == "rocksdb under an unreadable parent":
        data.chmod(0)
    return store


def _supported(layout: str, major: int | None, scheme: str, *, convert: bool) -> bool:
    """The starts Sibyl supports: 3.x held on RocksDB, 4.x on SurrealKV, one opted-in conversion."""
    if major == _HELD_MAJOR and scheme == "rocksdb":
        return layout in {"empty", "missing", "rocksdb"}
    if major == _SUPPORTED_MAJOR and scheme == "surrealkv":
        return layout in {"empty", "missing", "surrealkv", "converted"} or (
            convert and layout in {"rocksdb", "mixed"}
        )
    return False


@pytest.mark.parametrize("layout", _GUARD_LAYOUTS)
def test_store_guard_starts_only_supported_pairings(tmp_path: Path, layout: str) -> None:
    """Run the guard itself over every image, path and opt-in against one directory layout."""
    sh = shutil.which("sh")
    assert sh is not None
    script = tmp_path / "guard.sh"
    script.write_text(SURREAL_STORE_GUARD, encoding="utf-8")
    cases = [
        (image, major, scheme, convert)
        for image, major in _GUARD_IMAGES.items()
        for scheme in _GUARD_STORES
        for convert in (False, True)
    ]
    surprises = []
    for index, (image, major, scheme, convert) in enumerate(cases):
        root = tmp_path / f"case-{index}"
        root.mkdir()
        store = _lay_out(root, layout)
        result = subprocess.run(  # noqa: S603 - the repo's own guard, run by a fixed shell
            [sh, str(script)],
            cwd=root,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "SURREAL_IMAGE": image,
                "SURREAL_STORE": _GUARD_STORES[scheme].format(store=store),
                "SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X": "true" if convert else "",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        # A runner that reads through permissions, such as root, sees the RocksDB store inside.
        seen = (
            "rocksdb" if layout in _UNREADABLE and os.access(store, os.R_OK | os.X_OK) else layout
        )
        if (result.returncode == 0) != _supported(seen, major, scheme, convert=convert):
            surprises.append(
                f"{image} {scheme} opt-in={convert}: exit {result.returncode} {result.stderr}"
            )
        (root / "data").chmod(0o755)
        if store.exists():
            store.chmod(0o755)
    assert surprises == []
