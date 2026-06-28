#!/usr/bin/env python3
"""Propagate the canonical VERSION into every static deployment pin.

VERSION is the single source of truth for the Sibyl release version. Runtime
consumers already read it (package metadata, ``_version.py``, ``app.ts`` via
``NEXT_PUBLIC_VERSION``), but static deployment artifacts cannot: Helm chart
metadata, docker-compose image defaults, the Ansible role, and the copy-paste
examples in the deployment docs all carry a literal version string.

This tool stamps VERSION into all of them. In ``--check`` mode it instead
fails when any pin has drifted, so the release gate can guarantee that a
version bump actually reached every surface before a tag is cut.

Usage::

    uv run python tools/release/sync_versions.py           # write
    uv run python tools/release/sync_versions.py --check    # verify only
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TextIO

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = REPO_ROOT / "VERSION"

# A Sibyl release version: 1.2.3 or 1.2.3-rc.4. Scoped tightly so a surrounding
# context match never captures an unrelated version (e.g. SurrealDB's appVersion).
_VER = r"\d+\.\d+\.\d+(?:-rc\.\d+)?"

# Each target lists (pattern, replacement) pairs. Patterns capture the
# non-version context as groups so only the version token is rewritten. They
# are anchored to Sibyl-specific surroundings (chart fields, the SIBYL_* image
# defaults, sibyl image tags, the sibyl_version knob) to avoid touching other
# versions that share a file.
_Sub = tuple[str, str]


def _targets(version: str) -> dict[str, list[_Sub]]:
    v = version
    return {
        # Helm chart metadata (the wrapper chart tracks the app release).
        "charts/sibyl/Chart.yaml": [
            (rf"(?m)^(version: ){_VER}$", rf"\g<1>{v}"),
            (rf'(?m)^(appVersion: "){_VER}(")$', rf"\g<1>{v}\g<2>"),
        ],
        # SurrealDB subchart: only the wrapper version, never its appVersion
        # (that pins SurrealDB itself).
        "charts/surrealdb/Chart.yaml": [
            (rf"(?m)^(version: ){_VER}$", rf"\g<1>{v}"),
        ],
        # Compose image-tag defaults.
        "docker-compose.quickstart.yml": [
            (rf"(\$\{{SIBYL_IMAGE_TAG:-){_VER}(\}})", rf"\g<1>{v}\g<2>"),
        ],
        "infra/ansible/roles/sibyl/defaults/main.yml": [
            (rf'(?m)^(sibyl_version: "){_VER}(")$', rf"\g<1>{v}\g<2>"),
        ],
        "infra/ansible/roles/sibyl/files/docker-compose.yml": [
            (rf"(\$\{{SIBYL_VERSION:-){_VER}(\}})", rf"\g<1>{v}\g<2>"),
        ],
        # Deployment docs: copy-paste examples pinned to the release.
        "docs/cli/docker.md": [
            (rf"(--tag ){_VER}", rf"\g<1>{v}"),
        ],
        "docs/guide/installation.md": [
            (rf"(--tag ){_VER}", rf"\g<1>{v}"),
        ],
        "docs/deployment/ansible.md": [
            (rf"(`sibyl_version`\s*\|\s*`){_VER}", rf"\g<1>{v}"),
        ],
        "docs/deployment/helm-chart.md": [
            (rf"(?m)^(version: ){_VER}$", rf"\g<1>{v}"),
            (rf'(?m)^(appVersion: "){_VER}(")$', rf"\g<1>{v}\g<2>"),
            (rf'(tag: "){_VER}(")', rf"\g<1>{v}\g<2>"),
        ],
        "docs/deployment/kubernetes.md": [
            (rf'(tag: "){_VER}(")', rf"\g<1>{v}\g<2>"),
            (rf"(image\.tag=){_VER}", rf"\g<1>{v}"),
        ],
        "docs/deployment/monitoring.md": [
            (rf'("version": "){_VER}(")', rf"\g<1>{v}\g<2>"),
        ],
    }


def _read_version() -> str:
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not re.fullmatch(_VER, version):
        raise SystemExit(f"VERSION file holds an unexpected value: {version!r}")
    return version


def _apply(text: str, subs: list[_Sub]) -> tuple[str, list[int]]:
    counts: list[int] = []
    for pattern, repl in subs:
        text, n = re.subn(pattern, repl, text)
        counts.append(n)
    return text, counts


def emit(message: str, stream: TextIO = sys.stdout) -> None:
    stream.write(f"{message}\n")


def _emit_list(label: str, items: list[str]) -> None:
    emit(label)
    for rel_path in items:
        emit(f"  - {rel_path}")


_ANCHORS_LOST = "✗ Version anchors matched nothing (target reformatted?):"


def _report_check(version: str, drifted: list[str], unmatched: list[str]) -> int:
    if not (drifted or unmatched):
        emit(f"✓ All deployment pins match VERSION ({version}).")
        return 0
    if drifted:
        _emit_list(f"✗ Version pins out of sync with VERSION ({version}):", drifted)
    if unmatched:
        _emit_list(_ANCHORS_LOST, unmatched)
    emit("\nRun: moon run sync-versions")
    return 1


def _report_write(version: str, written: list[str], unmatched: list[str]) -> int:
    if written:
        _emit_list(f"Stamped VERSION ({version}) into {len(written)} file(s):", written)
    if unmatched:
        _emit_list(_ANCHORS_LOST, unmatched)
        return 1
    if not written:
        emit(f"✓ All deployment pins already match VERSION ({version}).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Propagate VERSION into static deployment pins.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify every pin matches VERSION; exit non-zero on drift.",
    )
    args = parser.parse_args()

    version = _read_version()
    targets = _targets(version)

    drifted: list[str] = []
    written: list[str] = []
    unmatched: list[str] = []

    for rel_path, subs in targets.items():
        path = REPO_ROOT / rel_path
        original = path.read_text(encoding="utf-8")
        updated, counts = _apply(original, subs)
        # A pattern that matched nothing means its anchor is gone (the target
        # was reformatted), so a stale pin would slip past the updated ==
        # original check below. Track it as a hard failure in both modes.
        if 0 in counts:
            unmatched.append(rel_path)
        if updated == original:
            continue
        if args.check:
            drifted.append(rel_path)
        else:
            path.write_text(updated, encoding="utf-8")
            written.append(rel_path)

    if args.check:
        return _report_check(version, drifted, unmatched)
    return _report_write(version, written, unmatched)


if __name__ == "__main__":
    sys.exit(main())
