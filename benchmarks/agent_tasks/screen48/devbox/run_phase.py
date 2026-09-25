"""Run one screen48 phase against the owned restored database on the eval host.

Environment is stamped before any ``sibyl`` module is imported, because the
core and API settings objects read ``SIBYL_*`` once at import. The phase then
runs with the owned database up, and the database is stopped in ``finally``
whatever happens.

``SIBYL_CONSOLIDATION_MAX_INPUT_CHARS`` is set to 800000, the study's measured
complete-evidence allowance. Unset, the budget follows the memory model: 40000
for most models, which would split the cohort into packets that cannot carry
complete evidence, and 1600000 for Opus 5 and 5.5. Pinning it keeps every phase
on the study's budget whatever model runs it.

Redis is a hard refusal rather than an override: a stray ``SIBYL_REDIS_*``
points the coordination backend at a broker this phase does not own, and the
consolidation work would leave the process.

Six phases are registered. ``cycle`` drives the consolidation cycle between
the two checkpoints; ``checkpoint0`` and ``checkpoint1`` prepare the frozen
schedule's own packs; ``probe`` prepares and runs the floor probe, a diagnostic
over other catalogued tasks that is never a cell of that schedule;
``repair_raw_embeddings`` gives the study organization's raw captures the
vectors a restore never wrote, so the raw_vector lane has rows to read before
a probe prepares the raw arm; and ``preflight`` qualifies the staging without
the database.

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
    # A whole cohort goes out in one non-streaming request. The study's first
    # paid cycle ran on the old 60 second default and lost 31 of its 57
    # transport attempts to read timeouts, so the phase pins the product's
    # raised memory-surface timeout instead of inheriting whatever is ambient.
    "SIBYL_LLM_MEMORY_TIMEOUT_SECONDS": "600",
    "SIBYL_CONSOLIDATION_MAX_INPUT_CHARS": "800000",
}

#: A named override for the pinned memory model, for a cycle deliberately run on
#: a different Opus. Only the listed models are accepted; the cycle's preflight
#: records whichever model actually ran and prices it from ``cycle.MODEL_PRICING``.
MEMORY_MODEL_OVERRIDE_ENV = "SCREEN48_MEMORY_MODEL"
MEMORY_MODEL_CHOICES = frozenset({"claude-opus-5", "claude-opus-5-5"})

#: Passed through from the ambient environment when present, never invented.
PASSTHROUGH_ENVIRONMENT = (
    "SIBYL_SURREAL_USERNAME",
    "SIBYL_SURREAL_PASSWORD",
    "SIBYL_EVAL_ISSUERS",
)

#: The API settings field that authorizes an eval experiment's outcome issuer.
#: The checkpoint phase requalifies the signed cohort archive through the
#: product's eval routes, which refuse an issuer that is not in this list.
EVAL_ISSUERS_ENV = "SIBYL_EVAL_ISSUERS"

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


def load_eval_issuers(path: Path) -> str:
    """Read an issuer file and return the JSON list the settings field expects.

    The file holds either one issuer object or a list of them; a single object
    is wrapped so the configured trust always parses as a list.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PhaseError(f"eval issuers file is unreadable: {path}: {exc}") from exc
    issuers = payload if isinstance(payload, list) else [payload]
    required = {
        "issuer_id",
        "organization_id",
        "experiment_id",
        "experiment_revision",
        "public_key_base64",
        "controller_policy_sha256",
    }
    for issuer in issuers:
        if not isinstance(issuer, dict) or not required <= set(issuer):
            raise PhaseError(f"eval issuers file lacks a complete issuer: {path}")
    return json.dumps(issuers, sort_keys=True, separators=(",", ":"))


def apply_environment(
    environ: dict[str, str] | None = None, *, eval_issuers_file: Path | None = None
) -> list[str]:
    """Stamp the phase environment and report the variable names that were set."""
    target = os.environ if environ is None else environ
    names: list[str] = []
    if eval_issuers_file is not None:
        target[EVAL_ISSUERS_ENV] = load_eval_issuers(eval_issuers_file)
    for key, value in PHASE_ENVIRONMENT.items():
        target[key] = value
        names.append(key)
    if override := target.get(MEMORY_MODEL_OVERRIDE_ENV):
        if override not in MEMORY_MODEL_CHOICES:
            raise PhaseError(
                f"{MEMORY_MODEL_OVERRIDE_ENV}={override!r} is not one of "
                f"{sorted(MEMORY_MODEL_CHOICES)}"
            )
        target["SIBYL_LLM_MEMORY_MODEL"] = override
        names.append(MEMORY_MODEL_OVERRIDE_ENV)
    names.extend(key for key in PASSTHROUGH_ENVIRONMENT if target.get(key))
    return sorted(names)


def _run_cycle_phase(output: Path, extra: Sequence[str], record: dict[str, Any]) -> int:
    from benchmarks.agent_tasks.screen48 import cycle

    del record
    return cycle.main(["--output", str(output), *extra])


def _probe_phase(output: Path, extra: Sequence[str], record: dict[str, Any]) -> int:
    """Run the floor probe, passing the host's own flags through.

    The probe prepares its packs off the live database, so this phase needs the
    owned container up exactly as a checkpoint phase does. What it produces is a
    diagnostic and never a cell of the frozen schedule, which is why it has its
    own phase rather than a flag on one of the checkpoints.
    """
    from benchmarks.agent_tasks.screen48 import probe

    del record
    return probe.main([*extra, "--output", str(output)])


def _intervention_phase(output: Path, extra: Sequence[str], record: dict[str, Any]) -> int:
    """Recompose a prepare-only probe's sealed packs and run the diagnostic arms.

    Every pack it runs was sealed by an earlier probe phase, so nothing here
    reads SurrealDB and the owned container stays as this lane found it.
    """
    from benchmarks.agent_tasks.screen48 import intervention

    del record
    return intervention.main([*extra, "--output", str(output)])


def _repair_raw_embeddings_phase(output: Path, extra: Sequence[str], record: dict[str, Any]) -> int:
    """Embed the study organization's raw captures that carry no vector.

    The restored study database holds its 233 originals exactly as the archive
    wrote them, and an archive carries no embeddings, so every raw_vector read
    over it returned nothing and the raw arm was BM25 alone. This phase runs
    the product's own repair against the owned database and keeps its receipt
    beside the phase record. It refuses to count as done unless the repair
    actually completed: a pass with no provider or a dimension mismatch leaves
    the lane exactly as unembedded as it found it, and a probe prepared on top
    of that would seal the same defect again.
    """
    import asyncio
    from dataclasses import asdict

    from benchmarks.agent_tasks.screen48 import contract

    from sibyl_core.services.content_client import close_shared_surreal_content_client
    from sibyl_core.services.content_models import configured_raw_memory_embedding_provider
    from sibyl_core.services.content_raw_embedding_repair import (
        REPAIR_COMPLETED,
        repair_raw_capture_embeddings,
    )

    if extra:
        raise PhaseError(f"repair_raw_embeddings takes no flags: {' '.join(extra)}")
    provider = configured_raw_memory_embedding_provider()
    receipt: dict[str, Any] = {
        "organization_id": contract.ORGANIZATION_ID,
        "provider": None if provider is None else asdict(provider.metadata),
        "started_at": datetime.now(UTC).isoformat(),
    }

    async def run() -> Any:
        try:
            return await repair_raw_capture_embeddings(contract.ORGANIZATION_ID)
        finally:
            # The repair rides the shared client; leave no pool socket open on
            # a closed loop when main() stops the container right after this.
            await close_shared_surreal_content_client()

    result = asyncio.run(run())
    receipt["result"] = asdict(result)
    # Completed is the repair's word for "the walk finished", not "every row
    # now carries a vector": a revoked key or a rejected write lands in
    # failed, a contended row in pending, and status stays completed. The
    # lane is only fair when nothing is left behind; a walk that had nothing
    # to do is fine.
    embedded_everything = (
        result.status == REPAIR_COMPLETED and result.failed == 0 and result.pending == 0
    )
    receipt["embedded_everything"] = embedded_everything
    receipt["finished_at"] = datetime.now(UTC).isoformat()
    record["repair_raw_embeddings"] = receipt
    output.mkdir(parents=True, exist_ok=True)
    (output / "repair.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if embedded_everything else 1


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
        "schedule_content_sha256": checkpoints.schedule_catalog_content_sha256(),
    }


#: ``--tokenizer-assets`` and ``--prior-root`` reach the checkpoint phases as
#: unparsed extras, the same way the cycle phase receives its own flags. The
#: probe phase takes its own flags the same way, ``--task-ids`` included.
PHASES: dict[str, PhaseRunner] = {
    "cycle": _run_cycle_phase,
    "checkpoint0": _checkpoint_phase(0),
    "checkpoint1": _checkpoint_phase(1),
    "preflight": _preflight_phase,
    "probe": _probe_phase,
    "intervention": _intervention_phase,
    "repair_raw_embeddings": _repair_raw_embeddings_phase,
}

#: Phases that read nothing out of SurrealDB, so the owned container stays as
#: this lane found it.
NO_DATABASE_PHASES = frozenset({"preflight", "intervention"})


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
    parser.add_argument("--eval-issuers-file", type=Path, default=None)
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
        record["environment_keys_set"] = apply_environment(eval_issuers_file=args.eval_issuers_file)
        record["memory_model"] = os.environ.get("SIBYL_LLM_MEMORY_MODEL")
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
