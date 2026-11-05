"""SurrealDB service pieces shared by the CLI's compose runtimes."""

import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
import yaml
from packaging.version import InvalidVersion, Version
from rich.markup import escape

from sibyl_cli.common import error, info, success
from sibyl_core.backends.surreal.server_version import SURREAL_SERVER_IMAGE
from sibyl_core.backends.surreal.server_version import (
    SURREAL_STORAGE_ENV as SURREAL_STORAGE_ENV,  # both runtimes' surrealdb environment
)

# The SurrealDB server this CLI release runs. Both runtimes reference it through
# SIBYL_SURREAL_IMAGE, so an operator can pin another image without editing the
# compose file, and both upgrade commands move older defaults on the same major
# up to it.
SURREAL_IMAGE = SURREAL_SERVER_IMAGE
SURREAL_IMAGE_REFERENCE = f"${{SIBYL_SURREAL_IMAGE:-{SURREAL_IMAGE}}}"
MANAGED_SURREAL_IMAGE = re.compile(r"\$\{SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:(?P<tag>[^}]+)\}")
# 4.0 has no RocksDB, so its stores are SurrealKV. The path, its environment
# (SURREAL_STORAGE_ENV), and the image move together: a 3.x server given this
# path on a 3.x RocksDB directory starts on a new, empty database beside the
# data, and 4.0 refuses a 3.x store it was not told to convert.
SURREAL_STORE_PATH = "surrealkv:///data/sibyl.db"
SURREAL_VOLUME = "sibyl_surreal"
SURREAL_MAJOR_RUNBOOK = "https://hyperb1iss.github.io/sibyl/deployment/surrealdb-4-upgrade.html"


def surreal_version(tag: str) -> Version | None:
    try:
        return Version(tag.removeprefix("v"))
    except InvalidVersion:
        return None


def shipped_surreal_tag() -> str:
    """The tag of the SurrealDB server this CLI runs, such as `v4.0.0`."""
    return SURREAL_IMAGE.rpartition(":")[2]


def _managed_tag(image: str) -> str | None:
    """The tag inside a CLI-written `${SIBYL_SURREAL_IMAGE:-...}` default, else None."""
    match = MANAGED_SURREAL_IMAGE.fullmatch(image)
    return match["tag"] if match else None


def held_surreal_tag(image: str) -> str | None:
    """The tag of a CLI-written default on an older SurrealDB major, else None.

    Crossing a major converts the store one way, so upgrades hold such a
    default where it is and only `upgrade --surreal-major` moves it.
    """
    tag = _managed_tag(image)
    current = surreal_version(tag) if tag else None
    shipped = surreal_version(shipped_surreal_tag())
    if current is None or shipped is None or current.major >= shipped.major:
        return None
    return tag


def upgraded_surreal_image(image: str) -> str | None:
    """The SurrealDB image an upgrade writes in place of `image`, or None to keep it.

    Only a CLI-written `${SIBYL_SURREAL_IMAGE:-...}` default on the major this
    CLI runs, at or below its server, moves. An upgrade never takes SurrealDB
    backwards, never replaces an image someone wrote by hand, and never
    crosses a major (see `held_surreal_tag`).
    """
    if image == SURREAL_IMAGE_REFERENCE:
        return image
    tag = _managed_tag(image)
    current = surreal_version(tag) if tag else None
    shipped = surreal_version(shipped_surreal_tag())
    if current is None or shipped is None or current > shipped or current.major < shipped.major:
        return None
    return SURREAL_IMAGE_REFERENCE


def surreal_kept_message(image: str, runtime: str) -> str:
    """Why an upgrade leaves the SurrealDB service as written, worded for the user."""
    held = held_surreal_tag(image)
    if held is None:
        return f"Leaving the SurrealDB image {escape(image)} as written; this CLI runs {SURREAL_IMAGE}."
    return (
        f"SurrealDB stays on {escape(held)}. Moving to {shipped_surreal_tag()} converts its data in "
        f"place and cannot be undone; run sibyl {runtime} upgrade --surreal-major when you "
        "are ready."
    )


def surreal_image_override(env_file: Path) -> str | None:
    """The SIBYL_SURREAL_IMAGE value Compose interpolates, if one is set.

    The shell environment wins over the env file, as it does in Compose.
    """
    override = os.environ.get("SIBYL_SURREAL_IMAGE")
    if override:
        return override
    try:
        lines = env_file.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        key, sep, value = line.removeprefix("export ").partition("=")
        value = value.strip().strip("\"'")
        if sep and key.strip() == "SIBYL_SURREAL_IMAGE" and value:
            return value
    return None


def surreal_data_mount() -> dict[str, Any]:
    """Prevent image copy-up from replacing initialized volume ownership."""
    return {
        "type": "volume",
        "source": SURREAL_VOLUME,
        "target": "/data",
        "volume": {"nocopy": True},
    }


def surreal_healthcheck() -> dict[str, Any]:
    """Readiness that leaves room for 4.0 to convert a 3.x store.

    Nothing listens while a store converts, and failures inside the start
    period do not count; a fresh start still turns healthy at its first
    passing check.
    """
    return {
        "test": ["CMD", "/surreal", "is-ready", "--endpoint", "http://localhost:8000"],
        "interval": "5s",
        "timeout": "3s",
        "retries": 5,
        "start_period": "10m",
    }


def surreal_volume_initializer() -> dict[str, Any]:
    """Initialize only the mount root; preserve existing database file metadata."""
    return {
        "image": "busybox:1.37",
        "container_name": "sibyl-surreal-init",
        "user": "0:0",
        # The official SurrealDB image uses distroless nonroot (65532:65532).
        "command": ["chown", "65532:65532", "/data"],
        "volumes": [surreal_data_mount()],
        "network_mode": "none",
        "read_only": True,
        "cap_drop": ["ALL"],
        "cap_add": ["CHOWN"],
        "security_opt": ["no-new-privileges:true"],
        "restart": "no",
    }


# ============================================================================
# Crossing a SurrealDB major (`upgrade --surreal-major`)
# ============================================================================


@dataclass(frozen=True)
class UpgradeStep:
    """One command of a SurrealDB major upgrade, and what it does in words."""

    summary: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class SurrealMajorSteps:
    """How an upgrade path moves a held store.

    `before` runs while the compose file still describes the held service,
    `service` then replaces the surrealdb service in it, and `after` runs on
    the new service and ends with the stack started.
    """

    before: tuple[UpgradeStep, ...]
    service: dict[str, Any]
    after: tuple[UpgradeStep, ...]


def pull_image_step(image: str) -> UpgradeStep:
    return UpgradeStep(f"Pulling {image}", ("docker", "pull", image))


def stop_services_step(compose: Sequence[str], services: Sequence[str]) -> UpgradeStep:
    return UpgradeStep(f"Stopping {', '.join(services)}", (*compose, "stop", *services))


def backup_volume_step(volume: str, backup: str) -> UpgradeStep:
    """Copy `volume` into `backup`, refusing when `backup` already holds data.

    Run it with SurrealDB stopped so the copy is a consistent store; a copy
    taken before the move is the only way back across a major.
    """
    script = (
        'if [ -n "$(ls -A /backup)" ]; then echo "$0 already holds data" >&2; exit 1; fi; '
        "cp -a /data/. /backup/"
    )
    return UpgradeStep(
        f"Copying the {volume} volume to {backup}",
        (
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{volume}:/data:ro",
            "-v",
            f"{backup}:/backup",
            "busybox:1.37",
            "sh",
            "-c",
            script,
            backup,
        ),
    )


def surreal_major_upgrade_steps(
    *, compose: Sequence[str], writers: Sequence[str], service: dict[str, Any]
) -> SurrealMajorSteps | None:
    """The upgrade path that moves a held store to this CLI's SurrealDB major.

    `compose` is the runtime's `docker compose -f ... --env-file ...` prefix,
    `writers` the services that write to SurrealDB, and `service` the held
    surrealdb service as written. A path starts with
    `pull_image_step(SURREAL_IMAGE)` and `stop_services_step(compose, writers)`,
    so the new image is local and nothing writes before the store is touched,
    and copies the store with `backup_volume_step` before it changes. None
    means no path is wired, and the command refuses with nothing changed.
    """
    # PENDING-UPGRADE-PATH-DECISION: the 4.0 disk lane picks in-place conversion or export/import.
    return None


@dataclass(frozen=True)
class SurrealMajorUpgrade:
    """A settled `upgrade --surreal-major`: the held tag and the path's steps."""

    held: str
    compose_file: Path
    steps: SurrealMajorSteps


def plan_surreal_major_upgrade(
    compose_file: Path, env_file: Path, compose: Sequence[str]
) -> SurrealMajorUpgrade | None:
    """Settle `upgrade --surreal-major` before anything is pulled, stopped, or written.

    Returns None when SurrealDB is not held on an older major, so there is
    nothing to do. Exits with nothing changed when the compose file cannot be
    read, SIBYL_SURREAL_IMAGE would override the move, or no path is wired.
    """
    try:
        services = (yaml.safe_load(compose_file.read_text()) or {})["services"]
        service = services["surrealdb"]
        image = service["image"]
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        error(f"Could not read the SurrealDB service from {compose_file}. Nothing changed.")
        raise typer.Exit(1) from None
    held = held_surreal_tag(image) if isinstance(image, str) else None
    if held is None:
        info(
            f"SurrealDB ({escape(str(image))}) is not on an older major version this CLI "
            "moves, so there is nothing to do."
        )
        return None
    if override := surreal_image_override(env_file):
        error(
            f"SIBYL_SURREAL_IMAGE pins SurrealDB to {escape(override)}, which would override "
            f"the move. Nothing changed. Remove it from the shell and {env_file} first."
        )
        raise typer.Exit(1)
    writers = [name for name in ("api", "worker", "web") if name in services]
    steps = surreal_major_upgrade_steps(compose=compose, writers=writers, service=service)
    if steps is None:
        error(
            f"Moving SurrealDB from {escape(held)} to {shipped_surreal_tag()} is not available in "
            "this CLI yet. Nothing changed."
        )
        info(f"The operator runbook covers the move: {SURREAL_MAJOR_RUNBOOK}")
        raise typer.Exit(1)
    return SurrealMajorUpgrade(held, compose_file, steps)


def _run_steps(steps: Sequence[UpgradeStep]) -> None:
    for step in steps:
        info(step.summary)
        if subprocess.run(list(step.command), check=False).returncode != 0:
            error(f"The SurrealDB move stopped: {step.summary} failed.")
            info(f"The operator runbook covers where to go from here: {SURREAL_MAJOR_RUNBOOK}")
            raise typer.Exit(1)


def run_surreal_major_upgrade(upgrade: SurrealMajorUpgrade, *, yes: bool) -> None:
    """Confirm, then run the path's steps in order, stopping at the first failure."""
    target = shipped_surreal_tag()
    if not yes and not typer.confirm(
        f"Move SurrealDB from {upgrade.held} to {target}? Its data is converted and "
        "cannot be moved back.",
        default=False,
    ):
        raise typer.Abort()
    _run_steps(upgrade.steps.before)
    config = yaml.safe_load(upgrade.compose_file.read_text())
    config["services"]["surrealdb"] = upgrade.steps.service
    upgrade.compose_file.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    _run_steps(upgrade.steps.after)
    success(f"SurrealDB now runs {target}")
