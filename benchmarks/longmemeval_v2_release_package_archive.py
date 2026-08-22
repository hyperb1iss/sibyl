"""Deterministic content archive for official LongMemEval-V2 packages."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import PurePosixPath
from typing import Any

from benchmarks import longmemeval_v2_release_state as state
from benchmarks.longmemeval_v2_release_inputs import StagePlanError, require_exact_keys

MANIFEST_NAME = "PACKAGE_MANIFEST.json"
MANIFEST_SCHEMA_VERSION = "sibyl-longmemeval-v2-package-manifest-v1"
MANIFEST_KEYS = frozenset({"schema_version", "directories", "files", "package_manifest_sha256"})
MAX_OBJECT_BYTES = 2 * 1024 * 1024 * 1024
MAX_TAR_NAME_BYTES = 100
CANONICAL_FILE_MODE = 0o444


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")


def member_binding(name: str, content: bytes) -> dict[str, Any]:
    return {
        "path": name,
        "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
        "size_bytes": len(content),
    }


def _require_member_name(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or path.as_posix() != name
        or any(part in {"", ".", ".."} for part in path.parts)
        or len(name.encode("utf-8")) > MAX_TAR_NAME_BYTES
    ):
        raise StagePlanError("official package object member name is unsafe")


def _manifest(files: dict[str, bytes]) -> dict[str, Any]:
    entries = [member_binding(name, files[name]) for name in sorted(files)]
    directories = sorted(
        {
            parent.as_posix()
            for name in files
            for parent in PurePosixPath(name).parents
            if parent != PurePosixPath(".")
        }
    )
    return state.sealed(
        {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "directories": directories,
            "files": entries,
        },
        "package_manifest_sha256",
    )


def build_package_object(files: dict[str, bytes]) -> tuple[bytes, dict[str, Any]]:
    """Build reproducible gzip/tar bytes from one exact fd-owned inventory."""

    if not files or MANIFEST_NAME in files:
        raise StagePlanError("official package object inventory is invalid")
    for name in files:
        _require_member_name(name)
    manifest = _manifest(files)
    members = {**files, MANIFEST_NAME: _json_bytes(manifest)}
    target = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=target, mode="wb", filename="", mtime=0) as compressed,
        tarfile.open(
            fileobj=compressed,
            mode="w",
            format=tarfile.USTAR_FORMAT,
        ) as archive,
    ):
        for name in sorted(members):
            content = members[name]
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mode = CANONICAL_FILE_MODE
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    result = target.getvalue()
    if len(result) > MAX_OBJECT_BYTES:
        raise StagePlanError("official package object exceeds the release bound")
    return result, manifest


def _read_members(content: bytes) -> dict[str, bytes]:
    if not content or len(content) > MAX_OBJECT_BYTES:
        raise StagePlanError("official package object size is invalid")
    members: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            infos = archive.getmembers()
            if [info.name for info in infos] != sorted(info.name for info in infos):
                raise StagePlanError("official package object order is not canonical")
            for info in infos:
                _require_member_name(info.name)
                if (
                    not info.isreg()
                    or info.mode != CANONICAL_FILE_MODE
                    or info.uid != 0
                    or info.gid != 0
                    or info.uname
                    or info.gname
                    or info.mtime != 0
                    or info.pax_headers
                    or info.name in members
                ):
                    raise StagePlanError("official package object metadata is not canonical")
                extracted = archive.extractfile(info)
                if extracted is None:
                    raise StagePlanError("official package object member is unreadable")
                members[info.name] = extracted.read()
    except (OSError, tarfile.TarError) as exc:
        raise StagePlanError("official package object is unreadable") from exc
    return members


def require_package_object(content: bytes) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Prove one package object has a reproducible archive and manifest."""

    members = _read_members(content)
    manifest_content = members.pop(MANIFEST_NAME, None)
    if manifest_content is None:
        raise StagePlanError("official package object manifest is missing")
    try:
        manifest = json.loads(manifest_content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StagePlanError("official package object manifest is invalid") from exc
    require_exact_keys(manifest, MANIFEST_KEYS, name="official package object manifest")
    expected = _manifest(members)
    if manifest != expected:
        raise StagePlanError("official package object manifest changed")
    rebuilt, rebuilt_manifest = build_package_object(members)
    if rebuilt != content or rebuilt_manifest != manifest:
        raise StagePlanError("official package object is not reproducible")
    return members, manifest
