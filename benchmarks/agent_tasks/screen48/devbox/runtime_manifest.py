"""Write and verify the private dependency runtime manifest ``runtime_pin`` reads.

``runtime_pin.verify`` (``recall/owners/runtime_pin.py``) takes the three-key
binding ``{root, manifest_sha256, interpreter}``, reads
``<root>/runtime-manifest.json``, and demands four things of it: that the file
still hashes to ``manifest_sha256``, that its own ``root`` and ``interpreter``
strings are byte-identical to the binding's, that every path under ``root``
except the manifest itself appears in ``files`` as either
``{"sha256", "mode"}`` or ``{"symlink"}``, and that no symlink resolves outside
``root``.

That last rule is what decides the layout. A venv whose ``bin/python`` points at
a system or user-level interpreter fails it, so the interpreter has to be staged
inside the runtime root alongside the environment, exactly the way the merged
runtime this lane inherits was built (``runtime/python`` beside ``runtime/.venv``).

Nothing here re-implements the check. ``write`` builds the inventory and then
hands the binding it just produced to ``runtime_pin.verify`` itself, so a
manifest that the real verifier would reject never reaches ``stage.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.agent_tasks.screen48.recall.owners import runtime_pin

MANIFEST_NAME = "runtime-manifest.json"
SCHEMA = "sibyl-screen48-runtime-manifest-v1"


class RuntimeManifestError(RuntimeError):
    """The staged runtime cannot be described or no longer matches."""


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def inventory(root: Path, manifest_path: Path) -> dict[str, dict[str, Any]]:
    """Walk ``root`` the way ``runtime_pin.verify`` walks it."""
    files: dict[str, dict[str, Any]] = {}
    for path in root.rglob("*"):
        if path == manifest_path:
            continue
        relative = str(path.relative_to(root))
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(root):
                raise RuntimeManifestError(
                    f"private dependency symlink escapes the runtime: {relative}"
                )
            files[relative] = {"symlink": os.readlink(path)}
        elif path.is_file():
            files[relative] = {
                "sha256": runtime_pin.sha(path),
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
        elif not path.is_dir():
            raise RuntimeManifestError(f"unsupported private dependency file type: {relative}")
    return files


def build(root: Path, interpreter: Path, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Describe the staged runtime as the manifest the pin reads."""
    root = Path(root)
    interpreter = Path(interpreter)
    if not root.is_absolute() or not interpreter.is_absolute():
        raise RuntimeManifestError("the runtime root and interpreter must be absolute")
    if not interpreter.resolve(strict=True).is_relative_to(root.resolve()):
        raise RuntimeManifestError(
            "the interpreter resolves outside the runtime root; stage the base "
            f"Python inside {root} and build the environment against it"
        )
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "root": str(root),
        "interpreter": str(interpreter),
    }
    manifest.update(extra or {})
    manifest["files"] = inventory(root, root / MANIFEST_NAME)
    return manifest


def serialize(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


def write(
    root: Path,
    interpreter: Path,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write ``<root>/runtime-manifest.json`` and prove it against the real pin."""
    root = Path(root)
    manifest_path = root / MANIFEST_NAME
    manifest = build(root, interpreter, extra)
    payload = serialize(manifest)
    with manifest_path.open("xb") as stream:
        stream.write(payload)
    binding = {
        "root": manifest["root"],
        "manifest_sha256": _sha(payload),
        "interpreter": manifest["interpreter"],
    }
    pinned = runtime_pin.verify(binding)
    return {
        "status": "runtime_manifest_written",
        "manifest": str(manifest_path),
        "dependency_runtime": binding,
        "files": pinned["files"],
    }


def verify(binding: dict[str, str], *, worker: bool = False) -> dict[str, Any]:
    """Re-run the pin over an already written manifest."""
    return runtime_pin.verify(dict(binding), worker=worker)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="screen48-runtime-manifest", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    writer = sub.add_parser("write", help="describe a staged runtime root")
    writer.add_argument("--root", type=Path, required=True)
    writer.add_argument("--interpreter", type=Path, required=True)
    checker = sub.add_parser("verify", help="re-check a staged runtime against its manifest")
    checker.add_argument("--root", type=Path, required=True)
    checker.add_argument("--interpreter", type=Path, required=True)
    checker.add_argument("--sha256", required=True)
    checker.add_argument("--worker", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "write":
        receipt = write(args.root, args.interpreter)
    else:
        receipt = {
            "status": "runtime_manifest_verified",
            "dependency_runtime": verify(
                {
                    "root": str(args.root),
                    "manifest_sha256": args.sha256,
                    "interpreter": str(args.interpreter),
                },
                worker=args.worker,
            ),
        }
    sys.stdout.write(json.dumps(receipt) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
