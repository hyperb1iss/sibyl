"""Write the ``SCREEN48_HOST_BINDING`` file for a staged runtime root.

Every value in the binding already exists somewhere on the eval host, and this
CLI only moves them into the one shape ``checkpoints.host_binding`` accepts:

``archive``
    The signed cohort tar and its digest, which ``archive_material`` re-hashes
    over the bytes it actually consumes.
``source``
    The restore ceremony's own ``source`` row, copied verbatim out of its
    ``source-config-v3.json``. ``verify_archive`` reads seven keys from it and
    ignores the rest, so the row travels whole rather than trimmed.
``owners``
    Exactly the six ``CurrentOwners`` constructor arguments. Four of them come
    from what ``stage.sh`` produced (the clean source export, its manifest, that
    manifest's digest and the commit it was exported from), one is the private
    dependency runtime ``stage.json`` recorded, and the last is the owned
    database URL the phases stamp into the environment.

No credential is read, named or written. The owner API key stays in its own
0600 file on the host and reaches a run through ``SCREEN48_OWNER_API_KEY``.
"""

# The checkpoint module pulls the whole recall stack in on import. Staging runs
# before any SIBYL_* variable is stamped, so that import stays inside the call
# that needs it.
# ruff: noqa: PLC0415

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

OWNED_DATABASE_URL = "ws://127.0.0.1:21642/rpc"
STAGE_RECEIPT_NAME = "stage.json"
SOURCE_DIRNAME = "source"
SOURCE_MANIFEST_NAME = "source-manifest.json"

#: The keys ``cohort_authority.verify_archive`` and ``qualify_originals`` read
#: out of the restore ceremony's source row. Extra keys ride along untouched.
REQUIRED_SOURCE_KEYS = frozenset(
    {
        "organization_id",
        "principal_id",
        "source_manifest_sha256",
        "experiment_revision",
        "issuer_id",
        "registered_attempts",
        "signed_admissions",
    }
)


class HostBindingError(RuntimeError):
    """The staged root or the restore config cannot produce a binding."""


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stage_receipt(runtime_root: Path) -> dict[str, Any]:
    """Read what ``stage.sh`` recorded about this root."""
    path = Path(runtime_root) / STAGE_RECEIPT_NAME
    receipt = json.loads(path.read_bytes())
    missing = [key for key in ("source_commit", "dependency_runtime") if not receipt.get(key)]
    if missing:
        raise HostBindingError(f"{path} records no {', '.join(missing)}; restage this root")
    runtime = receipt["dependency_runtime"]
    if set(runtime) != {"root", "manifest_sha256", "interpreter"}:
        raise HostBindingError(
            f"{path} dependency_runtime must be exactly root, manifest_sha256, interpreter"
        )
    return receipt


def source_row(source_config: Path) -> dict[str, Any]:
    """Lift the restore ceremony's ``source`` row out of its own input file."""
    config = json.loads(Path(source_config).read_bytes())
    row = config.get("source")
    if not isinstance(row, dict):
        raise HostBindingError(f"{source_config} carries no source row")
    missing = sorted(REQUIRED_SOURCE_KEYS - set(row))
    if missing:
        raise HostBindingError(f"{source_config} source row is missing {missing}")
    return dict(row)


def build(
    *,
    runtime_root: Path,
    archive: Path,
    archive_sha256: str,
    source_config: Path,
    owned_database_url: str = OWNED_DATABASE_URL,
    verify_archive_digest: bool = True,
) -> dict[str, Any]:
    """Assemble the binding from the staged root and the restore config."""
    runtime_root = Path(runtime_root)
    archive = Path(archive)
    receipt = stage_receipt(runtime_root)
    source = runtime_root / SOURCE_DIRNAME
    manifest = runtime_root / SOURCE_MANIFEST_NAME
    for path in (source, manifest):
        if not path.exists():
            raise HostBindingError(f"the staged root has no {path.name}: {path}")
    if verify_archive_digest:
        actual = file_digest(archive)
        if actual != archive_sha256:
            raise HostBindingError(f"archive {archive} hashes to {actual}, not {archive_sha256}")
    return {
        "archive": {"path": str(archive), "sha256": archive_sha256},
        "source": source_row(source_config),
        "owners": {
            "source": str(source),
            "source_manifest": str(manifest),
            "source_manifest_sha256": file_digest(manifest),
            "source_commit": receipt["source_commit"],
            "dependency_runtime": dict(receipt["dependency_runtime"]),
            "owned_database_url": owned_database_url,
        },
    }


def validate(path: Path) -> dict[str, Any]:
    """Re-read the written file through the checkpoint phase's own loader."""
    from benchmarks.agent_tasks.screen48 import checkpoints, contract

    binding = checkpoints.host_binding(path)
    source = binding["source"]
    if (source["organization_id"], source["principal_id"]) != (
        contract.ORGANIZATION_ID,
        contract.PRINCIPAL_ID,
    ):
        raise HostBindingError("the source row names another organization or principal")
    return binding


def write(out: Path, binding: dict[str, Any]) -> dict[str, Any]:
    out = Path(out)
    payload = json.dumps(binding, indent=2, sort_keys=True) + "\n"
    with out.open("x", encoding="utf-8") as stream:
        stream.write(payload)
    out.chmod(0o600)
    return {
        "status": "host_binding_written",
        "path": str(out),
        "archive": binding["archive"]["path"],
        "owners": {
            key: value for key, value in binding["owners"].items() if key != "dependency_runtime"
        },
        "dependency_runtime": binding["owners"]["dependency_runtime"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="screen48-host-binding", description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--owned-database-url", default=OWNED_DATABASE_URL)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--skip-archive-digest",
        action="store_true",
        help="do not re-hash the 400MB cohort archive while writing the binding",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="do not re-read the written file through the checkpoint loader",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    binding = build(
        runtime_root=args.runtime_root,
        archive=args.archive,
        archive_sha256=args.archive_sha256,
        source_config=args.source_config,
        owned_database_url=args.owned_database_url,
        verify_archive_digest=not args.skip_archive_digest,
    )
    receipt = write(args.out, binding)
    if not args.skip_validation:
        validate(args.out)
        receipt["validated"] = True
    sys.stdout.write(json.dumps(receipt) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
