"""SurrealDB service pieces shared by the CLI's compose runtimes."""

import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import typer
import yaml
from packaging.version import InvalidVersion, Version
from rich.markup import escape

from sibyl_cli.common import console, error, info, success, warn
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
BUSYBOX_IMAGE = "busybox:1.37"
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
        f"place, one way; run sibyl {runtime} upgrade --surreal-major when you are ready."
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
        "image": BUSYBOX_IMAGE,
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

# The store path every 3.x CLI wrote, which 4.0 converts in place.
SURREAL_3X_STORE_PATH = "rocksdb:///data/sibyl.db"
SURREAL_BACKUP_VOLUME = "sibyl_surreal_3x_backup"
# Lets 4.0 open a 3.x store and convert it; only the converting start carries it.
SURREAL_CONVERT_ENV = {"SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X": "true"}
_KIB_PER_GIB = 1024 * 1024
_SURREAL_READY = ("exec", "-T", "surrealdb", "/surreal", "is-ready", "--endpoint")
_SURREAL_ENDPOINT = "http://localhost:8000"


@dataclass(frozen=True)
class SurrealMajorUpgrade:
    """A settled `upgrade --surreal-major`: the runtime's stack and the major it holds."""

    runtime: str
    held: str
    compose_file: Path
    compose: tuple[str, ...]
    writers: tuple[str, ...]

    @property
    def held_compose_file(self) -> Path:
        """The compose file as it ran the held major, saved for a rollback."""
        return self.compose_file.with_name(f"{self.compose_file.name}.surreal3x.bak")


def _image_major(image: object) -> int | None:
    if not isinstance(image, str):
        return None
    version = surreal_version(_managed_tag(image) or image.rpartition(":")[2])
    return version.major if version else None


def _keeps_older_major(compose_file: Path) -> bool:
    """Whether a compose file still runs an older SurrealDB major than this CLI's."""
    try:
        image = yaml.safe_load(compose_file.read_text())["services"]["surrealdb"]["image"]
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        return False
    major, shipped = _image_major(image), _image_major(SURREAL_IMAGE)
    return major is not None and shipped is not None and major < shipped


def _older_release(image: object, release_tag: str | None) -> str | None:
    """The image's tag when it is a release older than `release_tag`, else None."""
    tag = image.rpartition(":")[2] if isinstance(image, str) else ""
    try:
        return tag if release_tag and Version(tag) < Version(release_tag) else None
    except InvalidVersion:
        return None


def _refuse(reason: str) -> NoReturn:
    error(f"{escape(reason)} Nothing changed.")
    raise typer.Exit(1)


def plan_surreal_major_upgrade(
    runtime: str,
    compose_file: Path,
    env_file: Path,
    compose: Sequence[str],
    known_compose_files: Sequence[Path],
    *,
    release_tag: str | None,
) -> SurrealMajorUpgrade | None:
    """Settle `upgrade --surreal-major` before anything is pulled, stopped, or written.

    Returns None when SurrealDB is not held on an older major, so there is
    nothing to do. Exits with nothing changed when the compose file cannot be
    read or no longer opens the 3.x store, the API it pins is a release older
    than `release_tag` (this CLI's), SIBYL_SURREAL_IMAGE would override the
    move, or another runtime would open the converted volume on 3.x.
    """
    try:
        services = (yaml.safe_load(compose_file.read_text()) or {})["services"]
        service = services["surrealdb"]
        image = service["image"]
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        _refuse(f"Could not read the SurrealDB service from {compose_file}.")
    held = held_surreal_tag(image) if isinstance(image, str) else None
    if held is None:
        info(
            f"SurrealDB ({escape(str(image))}) is not on an older major version this CLI "
            "moves, so there is nothing to do."
        )
        return None
    command = service.get("command")
    if not isinstance(command, list) or command[-1:] != [SURREAL_3X_STORE_PATH]:
        _refuse(
            f"The SurrealDB command in {compose_file} does not open {SURREAL_3X_STORE_PATH}, "
            "so this CLI cannot convert it."
        )
    api_image = services["api"].get("image") if isinstance(services.get("api"), dict) else None
    if older := _older_release(api_image, release_tag):
        # The API that first starts on the new major must be the release that
        # rebuilds the indexes it needs.
        _refuse(
            f"Sibyl runs {older} here, older than this CLI's {release_tag}. Move it first "
            f"with sibyl {runtime} upgrade, then run this again."
        )
    if override := surreal_image_override(env_file):
        _refuse(
            f"SIBYL_SURREAL_IMAGE pins SurrealDB to {override}, which would override the "
            f"move. Remove it from the shell and {env_file} first."
        )
    for other in known_compose_files:
        if other.resolve() != compose_file.resolve() and _keeps_older_major(other):
            _refuse(
                f"{other} also runs SurrealDB on the shared {SURREAL_VOLUME} volume, on an "
                "older major; after the move it would open the converted data as an empty "
                "database. Move that file aside first (the data lives in the volume, not in it)."
            )
    writers = tuple(name for name in ("api", "worker", "web") if name in services)
    return SurrealMajorUpgrade(runtime, held, compose_file, tuple(compose), writers)


def run_surreal_major_upgrade(
    upgrade: SurrealMajorUpgrade, *, yes: bool, wait_for_api: Callable[[int], bool]
) -> None:
    """Convert the held store in place on this CLI's SurrealDB, then bring Sibyl back on it.

    Until the new service is written the store is untouched, so a failure
    brings the held stack back. From then on a failure says where the move
    stands and how to roll back from the cold copy. Nothing is ever deleted.
    """
    target = shipped_surreal_tag()
    if not yes and not typer.confirm(
        f"Move SurrealDB from {upgrade.held} to {target}? This converts its data in place, "
        f"one way. A cold copy is kept in {SURREAL_BACKUP_VOLUME}, and about twice the "
        "store's size must be free.",
        default=False,
    ):
        raise typer.Abort()
    gib = _cold_copy(upgrade) / _KIB_PER_GIB

    _write_surreal_service(upgrade, converting=True)
    info(f"Converting the store on SurrealDB {target}; nothing answers until it finishes")
    if _run((*upgrade.compose, "up", "-d", "--no-deps", "surrealdb")).returncode != 0:
        _stranded(upgrade, f"Docker Compose could not start SurrealDB {target}.", "surrealdb")
    limit = max(600, int(30 * gib) + 300)
    if not _wait_for_surreal(upgrade.compose, limit):
        _stranded(
            upgrade,
            f"SurrealDB did not finish converting within {_duration(limit)}; it may still be "
            "converting.",
            "surrealdb",
        )

    _write_surreal_service(upgrade, converting=False)
    info("Starting Sibyl; the API rebuilds the auth and content indexes before it serves")
    if _run((*upgrade.compose, "up", "-d")).returncode != 0:
        _stranded(upgrade, "Docker Compose could not start Sibyl on the converted store.", None)
    limit = max(600, int(60 * gib) + 300)
    if not wait_for_api(limit):
        _stranded(
            upgrade,
            f"The API did not report ready within {_duration(limit)}; it may still be "
            "rebuilding the auth and content indexes.",
            "api",
        )

    console.print()
    success(f"SurrealDB now runs {target}, and Sibyl serves on it.")
    info(
        "Each organization's graph rebuilds its indexes on its first use, so the first "
        "requests for each organization can be slow."
    )
    info("Once you have checked your data, reclaim the space the 3.x copies hold:")
    _print_commands(
        _busybox(f"{SURREAL_VOLUME}:/data", script="rm -rf /data/sibyl.db/_rocksdb_backup"),
        ("docker", "volume", "rm", SURREAL_BACKUP_VOLUME),
        ("rm", str(upgrade.held_compose_file)),
    )
    _print_rollback(upgrade, "Until then, to go back")


def _cold_copy(upgrade: SurrealMajorUpgrade) -> int:
    """Pull, stop, copy, save, and measure; the KiB the store uses.

    The store is untouched until this returns, so a failure brings the held
    stack back.
    """
    refuse_full = (
        f'if [ -n "$(ls -A /backup)" ]; then echo "{SURREAL_BACKUP_VOLUME} already holds data" '
        ">&2; exit 1; fi; cp -a /data/. /backup/"
    )
    steps = (
        (f"Pulling {SURREAL_IMAGE}", ("docker", "pull", SURREAL_IMAGE)),
        (
            f"Stopping {', '.join((*upgrade.writers, 'surrealdb'))}",
            (*upgrade.compose, "stop", *upgrade.writers, "surrealdb"),
        ),
        (
            f"Copying the {SURREAL_VOLUME} volume into {SURREAL_BACKUP_VOLUME}",
            _busybox(
                f"{SURREAL_VOLUME}:/data:ro", f"{SURREAL_BACKUP_VOLUME}:/backup", script=refuse_full
            ),
        ),
    )
    for index, (summary, command) in enumerate(steps):
        info(summary)
        if _run(command).returncode != 0:
            _bring_back(upgrade, f"{summary} failed.", copied=index == 2)
    shutil.copy2(upgrade.compose_file, upgrade.held_compose_file)
    info(
        f"Saved the compose file that runs SurrealDB {upgrade.held} as {upgrade.held_compose_file}"
    )

    measured = _run(
        _busybox(
            f"{SURREAL_VOLUME}:/data:ro",
            script="du -sk /data | cut -f1; df -Pk /data | awk 'NR==2 {print $4}'",
        ),
        capture=True,
    )
    try:
        used, available = (int(value) for value in measured.stdout.split())
    except ValueError:
        used = available = -1
    if measured.returncode != 0 or used < 0:
        _bring_back(upgrade, f"Could not measure the {SURREAL_VOLUME} volume.", copied=True)
    if available < 2 * used:
        _bring_back(
            upgrade,
            f"Converting a {_size(used)} store needs about {_size(2 * used)} free beside it, "
            f"and only {_size(available)} is.",
            copied=True,
        )
    return used


def _write_surreal_service(upgrade: SurrealMajorUpgrade, *, converting: bool) -> None:
    """Point the compose file's surrealdb service at this CLI's server and store."""
    config = yaml.safe_load(upgrade.compose_file.read_text())
    service = config["services"]["surrealdb"]
    service["image"] = SURREAL_IMAGE_REFERENCE
    service["command"] = [*service["command"][:-1], SURREAL_STORE_PATH]
    service["environment"] = {**SURREAL_STORAGE_ENV, **(SURREAL_CONVERT_ENV if converting else {})}
    service["healthcheck"] = surreal_healthcheck()
    upgrade.compose_file.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


def _wait_for_surreal(compose: Sequence[str], limit: int) -> bool:
    """Poll SurrealDB until it answers ready, noting progress every 30 seconds."""
    started = noted = time.monotonic()
    while _run((*compose, *_SURREAL_READY, _SURREAL_ENDPOINT), capture=True).returncode != 0:
        now = time.monotonic()
        if now - started >= limit:
            return False
        if now - noted >= 30:
            info(f"Still converting after {_duration(now - started)} (allowing {_duration(limit)})")
            noted = now
        time.sleep(5)
    return True


def _bring_back(upgrade: SurrealMajorUpgrade, reason: str, *, copied: bool = False) -> NoReturn:
    error(f"{escape(reason)} The store was not converted.")
    restart = (*upgrade.compose, "up", "-d")
    if _run(restart).returncode == 0:
        info(f"The stack runs SurrealDB {upgrade.held} again, as before. Nothing changed.")
    else:
        error("Docker Compose could not start the stack again. Start it with:")
        _print_commands(restart)
    if copied:
        info(f"Remove the copy in {SURREAL_BACKUP_VOLUME} before you run this again:")
        _print_commands(("docker", "volume", "rm", SURREAL_BACKUP_VOLUME))
    raise typer.Exit(1)


def _stranded(upgrade: SurrealMajorUpgrade, reason: str, service: str | None) -> NoReturn:
    error(escape(reason))
    logs = f"sibyl {upgrade.runtime} logs" + (f" {service}" if service else "")
    info(f"Follow it with: {logs}. The runbook covers what comes next: {SURREAL_MAJOR_RUNBOOK}")
    _print_rollback(upgrade, "To go back")
    raise typer.Exit(1)


def _print_rollback(upgrade: SurrealMajorUpgrade, lead: str) -> None:
    info(f"{lead} to SurrealDB {upgrade.held} from the cold copy (later writes are lost):")
    _print_commands(
        (*upgrade.compose, "down"),
        _busybox(
            f"{SURREAL_VOLUME}:/data",
            f"{SURREAL_BACKUP_VOLUME}:/backup:ro",
            script="rm -rf /data/* /data/.[!.]* && cp -a /backup/. /data/",
        ),
        ("cp", str(upgrade.held_compose_file), str(upgrade.compose_file)),
        (*upgrade.compose, "up", "-d"),
    )
    warn(
        f"Never start SurrealDB {upgrade.held} on the converted volume: it opens it as an "
        "empty database."
    )


def _busybox(*mounts: str, script: str) -> tuple[str, ...]:
    volumes = [arg for mount in mounts for arg in ("-v", mount)]
    return (
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        *volumes,
        BUSYBOX_IMAGE,
        "sh",
        "-c",
        script,
    )


def _run(command: Sequence[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), check=False, text=True, capture_output=capture)


def _print_commands(*commands: Sequence[str]) -> None:
    for command in commands:
        console.print(f"  {shlex.join(command)}", soft_wrap=True, markup=False, highlight=False)


def _duration(seconds: float) -> str:
    return f"{int(seconds)} s" if seconds < 120 else f"{int(seconds // 60)} min"


def _size(kib: int) -> str:
    if kib < _KIB_PER_GIB:
        return f"{kib / 1024:.0f} MiB"
    return f"{kib / _KIB_PER_GIB:.1f} GiB"
