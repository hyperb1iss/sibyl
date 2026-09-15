"""Verify the independently qualified dependency copy before execution."""

import hashlib
import json
import os
import stat
import sys
from pathlib import Path


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(binding, *, worker=False):
    if set(binding) != {"root", "manifest_sha256", "interpreter"} or not all(binding.values()):
        raise ValueError("explicit private dependency runtime binding required")
    ROOT = Path(binding["root"])
    MANIFEST_SHA = binding["manifest_sha256"]
    INTERPRETER = Path(binding["interpreter"])
    if not ROOT.is_absolute() or not INTERPRETER.resolve(strict=True).is_relative_to(
        ROOT.resolve()
    ):
        raise ValueError("private interpreter escapes runtime")
    manifest_path = ROOT / "runtime-manifest.json"
    if sha(manifest_path) != MANIFEST_SHA:
        raise ValueError("private dependency manifest changed")
    manifest = json.loads(manifest_path.read_bytes())
    if manifest["root"] != str(ROOT) or manifest["interpreter"] != str(INTERPRETER):
        raise ValueError("private dependency manifest identity differs")
    actual = {}
    for path in ROOT.rglob("*"):
        if path == manifest_path:
            continue
        relative = str(path.relative_to(ROOT))
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(ROOT):
                raise ValueError("private dependency symlink escapes runtime")
            actual[relative] = {"symlink": os.readlink(path)}
        elif path.is_file():
            actual[relative] = {"sha256": sha(path), "mode": stat.S_IMODE(path.stat().st_mode)}
        elif not path.is_dir():
            raise ValueError("unsupported private dependency file type")
    if actual != manifest["files"]:
        raise ValueError("private dependency inventory changed")
    if worker and (
        Path(sys.executable) != INTERPRETER
        or not sys.dont_write_bytecode
        or os.environ.get("PYTHONDONTWRITEBYTECODE") != "1"
    ):
        raise ValueError("worker interpreter or bytecode policy differs")
    return {"manifest_sha256": MANIFEST_SHA, "files": len(actual), "interpreter": str(INTERPRETER)}
