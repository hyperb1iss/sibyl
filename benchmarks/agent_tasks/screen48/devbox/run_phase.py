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

PHASE_ENVIRONMENT = {
    "SIBYL_SURREAL_URL": SURREAL_URL,
    "SIBYL_COORDINATION_BACKEND": "local",
    "SIBYL_LLM_MEMORY_PROVIDER": "anthropic",
    "SIBYL_LLM_MEMORY_MODEL": "claude-opus-5",
    "SIBYL_CONSOLIDATION_MAX_INPUT_CHARS": "800000",
}

#: Passed through from the ambient environment when present, never invented.
PASSTHROUGH_ENVIRONMENT = ("SIBYL_SURREAL_USERNAME", "SIBYL_SURREAL_PASSWORD")


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


def _run_cycle_phase(output: Path, extra: Sequence[str]) -> int:
    from benchmarks.agent_tasks.screen48 import cycle

    return cycle.main(["--output", str(output), *extra])


def _checkpoint_phase(checkpoint: int) -> Callable[[Path, Sequence[str]], int]:
    """Run one checkpoint's pack preparation, passing the host's own flags through."""

    def run(output: Path, extra: Sequence[str]) -> int:
        from benchmarks.agent_tasks.screen48 import checkpoints

        return checkpoints.main(["--checkpoint", str(checkpoint), "--output", str(output), *extra])

    return run


#: ``--tokenizer-assets`` and ``--prior-root`` reach the checkpoint phases as
#: unparsed extras, the same way the cycle phase receives its own flags.
PHASES: dict[str, Callable[[Path, Sequence[str]], int]] = {
    "cycle": _run_cycle_phase,
    "checkpoint0": _checkpoint_phase(0),
    "checkpoint1": _checkpoint_phase(1),
}


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
    container_id = args.container_id or owned_db.DEFAULT_CONTAINER_ID
    socket_path = args.socket_path or owned_db.DEFAULT_SOCKET_PATH

    record: dict[str, Any] = {
        "phase": args.phase,
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": None,
        "source_commit": None,
        "python_version": sys.version,
        "environment_keys_set": [],
        "container_id": container_id,
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
    try:
        record["container_inspect_before"] = owned_db.start(container_id, socket_path=socket_path)
        record["surreal_health"] = owned_db.wait_ready(SURREAL_URL, args.ready_timeout)
        exit_code = PHASES[args.phase](args.output, extra)
        record["status"] = "ok" if exit_code == 0 else "failed"
    except Exception as exc:  # the phase record is the evidence
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            record["container_inspect_after"] = owned_db.stop(container_id, socket_path=socket_path)
        except owned_db.OwnedDatabaseError as exc:
            record["stop_error"] = f"{type(exc).__name__}: {exc}"
        record["exit_code"] = exit_code
        record["finished_at"] = datetime.now(UTC).isoformat()
        _write_phase_record(args.output, record)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
