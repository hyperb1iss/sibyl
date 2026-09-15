"""Run one screen48 phase against the owned restored database on the eval host.

Environment is stamped before any ``sibyl`` module is imported, because the
core and API settings objects read ``SIBYL_*`` once at import. The phase then
runs with the owned database up, and the database is stopped in ``finally``
whatever happens.

``SIBYL_CONSOLIDATION_MAX_INPUT_CHARS`` is set to 800000, the study's measured
complete-evidence allowance. The product default is 40000, which would split the
cohort into packets that cannot carry complete evidence.

Redis is a hard refusal rather than an override: a stray ``SIBYL_REDIS_*``
points the coordination backend at a broker this phase does not own, and the
consolidation work would leave the process.

The ``preflight`` phase is the exception to all of that: it never starts the
container and never reads the owner key. It runs every qualification a
checkpoint runs before its first database read, so a staging mistake surfaces
in seconds rather than as a ``STATUS_ERROR`` receipt halfway through a paid run.

Invocation shape, from inside the staged source export::

    cd $ROOT/source
    PYTHONDONTWRITEBYTECODE=1 $ROOT/runtime/.venv/bin/python -B \\
        -m benchmarks.agent_tasks.screen48.devbox.run_phase preflight \\
        --output $ROOT/preflight-<utc>

Both the ``-B`` and the environment variable are load-bearing, not belt and
braces. ``runtime_pin.verify(..., worker=True)`` requires ``sys.executable`` to
be exactly the interpreter the binding names, ``sys.dont_write_bytecode`` to be
true and ``PYTHONDONTWRITEBYTECODE`` to be ``1``; and a single ``__pycache__``
directory written under either the source export or the runtime root makes the
second ``verify_owners()`` call at the end of ``qualify_originals`` disagree
with the first. ``uv run`` is not a substitute: it would resolve a project
environment inside the source tree, which is exactly the layout the pin rejects.

``_source_commit`` reports ``unknown`` under a clean export, which has no
``.git``. The commit that matters is the one the host binding carries, recorded
by ``stage.sh`` and re-checked against the source manifest by ``CurrentOwners``.
"""

# Imports of sibyl-facing modules stay inside functions so the phase
# environment is stamped before anything reads SIBYL_* settings.
# ruff: noqa: PLC0415

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]

SURREAL_URL = "ws://127.0.0.1:21642/rpc"

#: The checkpoint module's own environment variable, spelled here so the output
#: guard can find a binding without importing that module.
HOST_BINDING_ENV = "SCREEN48_HOST_BINDING"

PHASE_ENVIRONMENT = {
    "SIBYL_SURREAL_URL": SURREAL_URL,
    "SIBYL_COORDINATION_BACKEND": "local",
    "SIBYL_LLM_MEMORY_PROVIDER": "anthropic",
    "SIBYL_LLM_MEMORY_MODEL": "claude-opus-5",
    "SIBYL_CONSOLIDATION_MAX_INPUT_CHARS": "800000",
}

#: Passed through from the ambient environment when present, never invented.
PASSTHROUGH_ENVIRONMENT = ("SIBYL_SURREAL_USERNAME", "SIBYL_SURREAL_PASSWORD")

EXIT_OK = 0
EXIT_PREFLIGHT_FAILED = 2

#: A phase is handed its output directory, the flags this module did not parse,
#: and the record it may add its own evidence to before the record is sealed.
PhaseRunner = Callable[[Path, Sequence[str], dict[str, Any]], int]


class PhaseError(RuntimeError):
    """The phase could not be prepared or run."""


def assert_no_redis(environ: dict[str, str] | None = None) -> None:
    """Refuse to run while any Redis coordination variable is set."""
    source = os.environ if environ is None else environ
    offenders = sorted(key for key in source if key.startswith("SIBYL_REDIS_"))
    if offenders:
        raise PhaseError(
            "refusing to run with Redis coordination configured: " + ", ".join(offenders)
        )


def apply_environment(environ: dict[str, str] | None = None) -> list[str]:
    """Stamp the phase environment and report the variable names that were set."""
    target = os.environ if environ is None else environ
    names: list[str] = []
    for key, value in PHASE_ENVIRONMENT.items():
        target[key] = value
        names.append(key)
    names.extend(key for key in PASSTHROUGH_ENVIRONMENT if target.get(key))
    return sorted(names)


def _run_cycle_phase(output: Path, extra: Sequence[str], record: dict[str, Any]) -> int:
    from benchmarks.agent_tasks.screen48 import cycle

    del record
    return cycle.main(["--output", str(output), *extra])


def _checkpoint_phase(checkpoint: int) -> PhaseRunner:
    """Run one checkpoint's pack preparation, passing the host's own flags through."""

    def run(output: Path, extra: Sequence[str], record: dict[str, Any]) -> int:
        from benchmarks.agent_tasks.screen48 import checkpoints

        del record
        return checkpoints.main(["--checkpoint", str(checkpoint), "--output", str(output), *extra])

    return run


def qualified_roots(binding_path: Path | None) -> list[tuple[str, Path]]:
    """The two manifested roots named by the host binding, or nothing.

    Read as plain JSON on purpose. This runs before ``apply_environment``, and
    importing the checkpoint module here would pull the sibyl settings objects
    in before ``SIBYL_*`` is stamped. A binding that cannot be read yields no
    roots: the phase itself reports that far better than a guard can.
    """
    location = binding_path or Path(os.environ.get(HOST_BINDING_ENV, ""))
    try:
        owners = json.loads(Path(location).read_bytes())["owners"]
        return [
            ("source", Path(owners["source"])),
            ("runtime", Path(owners["dependency_runtime"]["root"])),
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def assert_output_outside_qualified_trees(output: Path, binding_path: Path | None = None) -> None:
    """Refuse an output directory inside a tree the owners will inventory.

    A receipt written under the source export or the runtime root changes the
    inventory ``CurrentOwners`` qualified, which surfaces much later as
    ``current_source_inventory_changed`` on a reused ``verify_owners``. The
    refusal has to land before any file is created, the phase record included,
    or the refusal itself is the mutation.
    """
    for name, root in qualified_roots(binding_path):
        if output.absolute().is_relative_to(root.absolute()):
            raise PhaseError(f"--output may not live inside the qualified {name} tree: {output}")


def _record_check(checks: list[dict[str, Any]], name: str, call: Callable[[], Any]) -> Any:
    """Run one preflight check and keep its outcome whether it passed or not."""
    try:
        detail = call()
    except Exception as exc:  # the phase record is the evidence
        checks.append({"check": name, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return None
    checks.append({"check": name, "status": "ok", "detail": detail})
    return detail


def _preflight_phase(output: Path, extra: Sequence[str], record: dict[str, Any]) -> int:
    """Qualify the staged runtime without the owned database or the owner key.

    Everything a checkpoint does before it reads a row is run here: the host
    binding is loaded through the checkpoint phase's own loader, the private
    dependency runtime is pinned as a worker, ``CurrentOwners`` qualifies the
    source tree and the live import graph, the cohort archive is re-hashed over
    the bytes ``archive_material`` consumes, and every hash-bound material file
    is rebound. None of those touch SurrealDB.

    What preflight cannot reach is named in ``not_covered`` rather than faked.
    ``CurrentAuthority`` needs an authenticated intake context, so it needs the
    owner key; ``qualify_originals`` reads the live ledger, the registered
    assignments and all 233 capture snapshots, so it needs the database.
    """
    from benchmarks.agent_tasks.screen48 import checkpoints, contract
    from benchmarks.agent_tasks.screen48.recall.owners import runtime_pin
    from benchmarks.agent_tasks.screen48.recall.owners.cohort_authority import archive_material
    from benchmarks.agent_tasks.screen48.recall.qualification import REQUIRED_OWNERS, CurrentOwners

    if extra:
        raise PhaseError(f"preflight takes no extra flags: {' '.join(extra)}")
    stated = record.get("host_binding")
    binding_path = Path(stated) if stated else None

    checks: list[dict[str, Any]] = []
    record["checks"] = checks
    record["database_started"] = False
    record["owner_api_key_read"] = False
    record["not_covered"] = {
        "current_authority": "needs the owner API key to authenticate an intake context",
        "qualify_originals": "needs the owned database for the ledger and the 233 snapshots",
    }
    record["output"] = str(output)

    # The binding is read exactly once. Everything that can raise stays inside a
    # _record_check callable, including the CurrentOwners constructor, which
    # resolves the source root strictly and so fails on precisely the staging
    # mistake this phase exists to name.
    loaded: dict[str, Any] = {}
    detail = _record_check(
        checks,
        "host_binding",
        lambda: _binding_detail(checkpoints, contract, binding_path, loaded),
    )
    if detail is None:
        return EXIT_PREFLIGHT_FAILED
    owners = loaded["binding"]["owners"]

    _record_check(
        checks,
        "dependency_runtime_pin",
        lambda: runtime_pin.verify(owners["dependency_runtime"], worker=True),
    )
    _record_check(checks, "required_owners_imported", lambda: _import_owners(REQUIRED_OWNERS))

    def qualify_owners() -> dict[str, Any]:
        # Constructed inside the check: __init__ resolves the source root
        # strictly, so a moved export fails here rather than past the capture.
        return CurrentOwners(**owners)()

    _record_check(checks, "current_owners_qualification", qualify_owners)
    _record_check(
        checks, "cohort_archive", lambda: _archive_detail(archive_material, loaded["binding"])
    )
    _record_check(checks, "material_pins", lambda: _material_detail(checkpoints, contract))

    failed = [check["check"] for check in checks if check["status"] != "ok"]
    record["failed_checks"] = failed
    return EXIT_OK if not failed else EXIT_PREFLIGHT_FAILED


def _binding_detail(
    checkpoints: Any,
    contract: Any,
    path: Path | None,
    loaded: dict[str, Any],
) -> dict[str, Any]:
    """Load the host binding and confirm it names the study's own cohort."""
    binding = checkpoints.host_binding(path)
    source = binding["source"]
    if (source["organization_id"], source["principal_id"]) != (
        contract.ORGANIZATION_ID,
        contract.PRINCIPAL_ID,
    ):
        raise PhaseError("the host binding names another organization or principal")
    owners = binding["owners"]
    loaded["binding"] = binding
    return {
        "archive": binding["archive"],
        "source_keys": sorted(source),
        "source_commit": owners["source_commit"],
        "source_root": owners["source"],
        "source_manifest_sha256": owners["source_manifest_sha256"],
        "owned_database_url": owners["owned_database_url"],
        "dependency_runtime_root": owners["dependency_runtime"]["root"],
    }


def _import_owners(required: frozenset[str]) -> dict[str, str]:
    """Import the owners the qualification pass insists are already loaded."""
    import importlib

    return {name: str(importlib.import_module(name).__file__) for name in sorted(required)}


def _archive_detail(archive_material: Callable[..., dict], binding: dict[str, Any]) -> dict:
    """Re-hash the signed cohort archive over the bytes the lane consumes."""
    archive = binding["archive"]
    material = archive_material(Path(archive["path"]), archive["sha256"])
    return {
        "sha256": archive["sha256"],
        "members": len(material),
        "roots": sorted(name for name in material if not name.startswith("attempts/")),
        "attempt_members": sum(name.startswith("attempts/") for name in material),
    }


def _material_detail(checkpoints: Any, contract: Any) -> dict[str, Any]:
    """Rebind every hash-pinned material file the checkpoint phase reads."""
    library, _validator = checkpoints.summary_library()
    geometry = contract.source_geometry()
    tasks = {task: len(contract.public_task(task)[1]) for task in contract.TASKS}
    return {
        "summary_library_references": len(library),
        "source_geometry_rows": len(geometry),
        "training_families": len({row["training_family"] for row in geometry}),
        "workspace_files_per_task": tasks,
        "schedule_catalog_sha256": checkpoints.schedule_catalog_sha256(),
    }


#: ``--tokenizer-assets`` and ``--prior-root`` reach the checkpoint phases as
#: unparsed extras, the same way the cycle phase receives its own flags.
PHASES: dict[str, PhaseRunner] = {
    "cycle": _run_cycle_phase,
    "checkpoint0": _checkpoint_phase(0),
    "checkpoint1": _checkpoint_phase(1),
    "preflight": _preflight_phase,
}

#: Phases that read nothing out of SurrealDB, so the owned container stays as
#: this lane found it.
NO_DATABASE_PHASES = frozenset({"preflight"})


def _source_commit() -> str:
    from benchmarks.git_provenance import git_provenance

    provenance = git_provenance(REPO_ROOT)
    return str(provenance["sibyl_commit"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="screen48-run-phase", description=__doc__)
    parser.add_argument("phase", choices=sorted(PHASES))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--container-id", default=None)
    parser.add_argument("--socket-path", default=None)
    parser.add_argument("--ready-timeout", type=float, default=120.0)
    parser.add_argument("--host-binding", type=Path, default=None)
    return parser


def _write_phase_record(output: Path, record: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    path = output / "phase.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def main(argv: Sequence[str] | None = None) -> int:
    from benchmarks.agent_tasks.screen48.devbox import owned_db

    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    needs_database = args.phase not in NO_DATABASE_PHASES
    try:
        assert_output_outside_qualified_trees(args.output, args.host_binding)
    except PhaseError as exc:
        # Deliberately no phase record: writing one is the very mutation this
        # refusal exists to prevent.
        sys.stderr.write(f"{exc}\n")
        return 2
    container_id = args.container_id or owned_db.DEFAULT_CONTAINER_ID
    socket_path = args.socket_path or owned_db.DEFAULT_SOCKET_PATH

    record: dict[str, Any] = {
        "phase": args.phase,
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": None,
        "source_commit": None,
        "python_version": sys.version,
        "environment_keys_set": [],
        "container_id": container_id if needs_database else None,
        "host_binding": str(args.host_binding) if args.host_binding else None,
        "container_inspect_before": None,
        "container_inspect_after": None,
        "status": "error",
        "exit_code": 2,
    }
    try:
        assert_no_redis()
        record["environment_keys_set"] = apply_environment()
        record["source_commit"] = _source_commit()
    except (PhaseError, OSError, ValueError) as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["finished_at"] = datetime.now(UTC).isoformat()
        _write_phase_record(args.output, record)
        sys.stderr.write(f"{exc}\n")
        return 2

    exit_code = 2
    record["database_required"] = needs_database
    try:
        if needs_database:
            record["container_inspect_before"] = owned_db.start(
                container_id, socket_path=socket_path
            )
            record["surreal_health"] = owned_db.wait_ready(SURREAL_URL, args.ready_timeout)
        exit_code = PHASES[args.phase](args.output, extra, record)
        record["status"] = "ok" if exit_code == 0 else "failed"
    except Exception as exc:  # the phase record is the evidence
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if needs_database:
            try:
                record["container_inspect_after"] = owned_db.stop(
                    container_id, socket_path=socket_path
                )
            except owned_db.OwnedDatabaseError as exc:
                record["stop_error"] = f"{type(exc).__name__}: {exc}"
        record["exit_code"] = exit_code
        record["finished_at"] = datetime.now(UTC).isoformat()
        _write_phase_record(args.output, record)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
