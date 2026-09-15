"""Write and verify the source manifest the checkpoint owners qualify against.

``CurrentOwners`` (``recall/qualification.py``) reads exactly three keys out of
this file: ``base_commit``, ``files`` (path relative to the source root mapped
to its sha256) and ``symlinks`` (path mapped to its raw link target). It walks
the live tree with ``rglob("*")``, takes symlinks before regular files, skips
directories, refuses anything else, and compares the result to the manifest. So
the manifest has to be produced by walking the tree exactly that way; this
module is that walk and nothing more.

``modes`` is recorded as well. No qualification branch reads it. It is here so
``verify`` can catch a permission change that the digests cannot see, the same
way the devbox's own staging verifier did.

The manifest is written outside the source root. A manifest inside the tree
would describe a file whose digest it cannot contain.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "sibyl-screen48-source-manifest-v1"

#: Directories that must never reach a staged source tree. ``.git`` mutates
#: under a plain read, ``.venv`` would drag 20k dependency files into the
#: application inventory, and bytecode caches appear the first time a phase
#: runs and would then read as "the source moved under the lane".
FORBIDDEN_NAMES = frozenset({".git", ".venv", "__pycache__"})


class SourceManifestError(RuntimeError):
    """The staged source tree cannot be described or no longer matches."""


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def inventory(root: Path) -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
    """Walk ``root`` the way ``CurrentOwners`` walks it.

    Returns the file digests, the symlink targets and the file modes, each
    keyed by the path relative to ``root``.
    """
    files: dict[str, str] = {}
    symlinks: dict[str, str] = {}
    modes: dict[str, int] = {}
    for path in root.rglob("*"):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            symlinks[rel] = str(path.readlink())
            if not path.resolve(strict=True).is_relative_to(root):
                raise SourceManifestError(f"source symlink escapes the tree: {rel}")
        elif path.is_file():
            files[rel] = _sha(path.read_bytes())
            modes[rel] = stat.S_IMODE(path.stat().st_mode)
        elif not path.is_dir():
            raise SourceManifestError(f"unsupported source file type: {rel}")
    return files, symlinks, modes


def assert_clean(root: Path) -> None:
    """Refuse a tree carrying a checkout, an environment or a bytecode cache."""
    for path in root.rglob("*"):
        if path.is_dir() and path.name in FORBIDDEN_NAMES:
            raise SourceManifestError(
                f"staged source is not a clean export: {path.relative_to(root)}"
            )


def build(root: Path, base_commit: str) -> dict[str, Any]:
    """Describe the staged tree as the manifest the owners read."""
    absolute = Path(root).absolute()
    if any(parent.is_symlink() for parent in (absolute, *absolute.parents)):
        raise SourceManifestError(f"the staged source path traverses a symlink: {root}")
    resolved = absolute.resolve(strict=True)
    files, symlinks, modes = inventory(resolved)
    return {
        "schema": SCHEMA,
        "base_commit": base_commit,
        "created_at": datetime.now(UTC).isoformat(),
        "source_root": str(resolved),
        "files": files,
        "symlinks": symlinks,
        "modes": modes,
        "file_count": len(files),
        "symlink_count": len(symlinks),
    }


def serialize(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


def write(root: Path, base_commit: str, out: Path) -> dict[str, Any]:
    """Write the manifest for ``root`` and report its own digest."""
    root = Path(root)
    out = Path(out)
    if out.resolve().is_relative_to(root.resolve(strict=True)):
        raise SourceManifestError(f"the manifest may not live inside the source tree: {out}")
    assert_clean(root)
    manifest = build(root, base_commit)
    payload = serialize(manifest)
    with out.open("xb") as stream:
        stream.write(payload)
    return {
        "status": "source_manifest_written",
        "source": manifest["source_root"],
        "manifest": str(out),
        "manifest_sha256": _sha(payload),
        "base_commit": base_commit,
        "files": manifest["file_count"],
        "symlinks": manifest["symlink_count"],
    }


def verify(
    root: Path,
    manifest_path: Path,
    *,
    expected_sha256: str | None = None,
    base_commit: str | None = None,
) -> dict[str, Any]:
    """Re-run every check the qualification pass runs over the source tree."""
    root = Path(root)
    manifest_path = Path(manifest_path)
    payload = manifest_path.read_bytes()
    digest = _sha(payload)
    if expected_sha256 is not None and digest != expected_sha256:
        raise SourceManifestError(f"source manifest changed: {manifest_path}")
    manifest = json.loads(payload)
    if base_commit is not None and manifest["base_commit"] != base_commit:
        raise SourceManifestError("source manifest names another base commit")
    files, symlinks, modes = inventory(root.resolve(strict=True))
    if files != manifest["files"]:
        raise SourceManifestError("source file inventory changed")
    if symlinks != manifest["symlinks"]:
        raise SourceManifestError("source symlink inventory changed")
    if "modes" in manifest and modes != manifest["modes"]:
        raise SourceManifestError("source file modes changed")
    return {
        "status": "source_manifest_verified",
        "source": str(root.resolve()),
        "manifest": str(manifest_path),
        "manifest_sha256": digest,
        "base_commit": manifest["base_commit"],
        "files": len(files),
        "symlinks": len(symlinks),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="screen48-source-manifest", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    writer = sub.add_parser("write", help="write the manifest for a staged source tree")
    writer.add_argument("--source", type=Path, required=True)
    writer.add_argument("--commit", required=True)
    writer.add_argument("--out", type=Path, required=True)
    checker = sub.add_parser("verify", help="re-check a staged tree against its manifest")
    checker.add_argument("--source", type=Path, required=True)
    checker.add_argument("--manifest", type=Path, required=True)
    checker.add_argument("--sha256", default=None)
    checker.add_argument("--commit", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "write":
        receipt = write(args.source, args.commit, args.out)
    else:
        receipt = verify(
            args.source,
            args.manifest,
            expected_sha256=args.sha256,
            base_commit=args.commit,
        )
    sys.stdout.write(json.dumps(receipt) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
