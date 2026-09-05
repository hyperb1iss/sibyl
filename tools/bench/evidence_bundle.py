"""Retain explicit benchmark inputs and outputs using the existing package format."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.longmemeval_v2_release_package_archive import (  # noqa: E402
    build_package_object,
    require_package_object,
)


def preserve(files: dict[str, Path], store: Path) -> Path:
    """Publish a deterministic bundle without replacing an existing object."""
    contents = {}
    for name, source in files.items():
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Evidence input must be a regular file: {source}")
        contents[name] = source.read_bytes()
    content, _ = build_package_object(contents)
    digest = hashlib.sha256(content).hexdigest()
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = store / f"{digest}.tar.gz"
    with tempfile.NamedTemporaryFile(dir=store, prefix=".evidence-", delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                if destination.is_symlink() or destination.read_bytes() != content:
                    raise ValueError("Existing evidence object does not match its digest") from None
        finally:
            temporary_path.unlink(missing_ok=True)
    return destination


def verify(bundle: Path, digest: str) -> dict[str, bytes]:
    """Verify transport identity and every archived member before reading evidence."""
    digest = digest.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Expected digest must be a lowercase SHA-256")
    content = bundle.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Evidence bundle SHA-256 mismatch")
    files, _ = require_package_object(content)
    return files


def restore(bundle: Path, digest: str, destination: Path) -> list[str]:
    """Restore a verified bundle into a new directory; never overwrite a prior run."""
    files = verify(bundle, digest)
    # The archive validator rejects traversal, links, and noncanonical member names.
    # Check file/directory collisions before creating anything at the destination.
    for name in files:
        if any(parent.as_posix() in files for parent in Path(name).parents):
            raise ValueError("Evidence inventory contains a file/directory collision")
    destination.mkdir(mode=0o700)
    try:
        for name, content in files.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as handle:
                os.chmod(target, 0o600)
                handle.write(content)
    except BaseException:
        # Only this call's exclusively created destination is eligible for cleanup.
        shutil.rmtree(destination)
        raise
    return sorted(files)


def _inventory(values: list[str]) -> dict[str, Path]:
    files = {}
    for value in values:
        name, separator, source = value.partition("=")
        if not separator or not name or not source or name in files:
            raise ValueError("Each --file needs a unique NAME=PATH")
        files[name] = Path(source)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    save = commands.add_parser("preserve", help="Bundle explicitly selected files locally")
    save.add_argument("--file", action="append", required=True, metavar="NAME=PATH")
    save.add_argument("--store", type=Path, required=True)
    for command in ("verify", "restore"):
        sub = commands.add_parser(command)
        sub.add_argument("--bundle", type=Path, required=True)
        sub.add_argument("--sha256", required=True)
        if command == "restore":
            sub.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "preserve":
            bundle = preserve(_inventory(args.file), args.store)
            result = {"bundle": str(bundle), "sha256": bundle.name.removesuffix(".tar.gz")}
        elif args.command == "verify":
            result = {"verified_files": sorted(verify(args.bundle, args.sha256))}
        else:
            result = {
                "restored_files": restore(args.bundle, args.sha256, args.destination),
                "destination": str(args.destination),
            }
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Evidence bundle failed: {exc}\n")
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
