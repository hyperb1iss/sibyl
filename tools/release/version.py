"""Canonical Sibyl release version grammar and normalization."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

# Public tags use ``v<canonical>``. Python package metadata uses the PEP 440
# spelling exposed by ``ReleaseVersion.pep440``. Every release consumer parses
# the same canonical input before it derives either form.
RELEASE_VERSION_PATTERN = (
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-rc\.(?:[1-9][0-9]*))?"
)
PEP440_VERSION_PATTERN = (
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:rc(?:[1-9][0-9]*))?"
)

_RELEASE_VERSION_RE = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-rc\.(?P<rc>[1-9][0-9]*))?$"
)


@dataclass(frozen=True, slots=True)
class ReleaseVersion:
    major: int
    minor: int
    patch: int
    rc: int | None = None

    @property
    def canonical(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-rc.{self.rc}" if self.rc is not None else base

    @property
    def pep440(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}rc{self.rc}" if self.rc is not None else base

    @property
    def tag(self) -> str:
        return f"v{self.canonical}"


def parse_release_version(value: str) -> ReleaseVersion:
    """Parse the only version grammar accepted by release mutation paths."""
    match = _RELEASE_VERSION_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"unsupported release version {value!r}; expected X.Y.Z or X.Y.Z-rc.N")
    rc = match.group("rc")
    return ReleaseVersion(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        rc=int(rc) if rc is not None else None,
    )


def pep440_version(value: str) -> str:
    return parse_release_version(value).pep440


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Sibyl release version.")
    parser.add_argument("version", help="Canonical version: X.Y.Z or X.Y.Z-rc.N")
    parser.add_argument(
        "--format",
        choices=("canonical", "pep440", "tag"),
        default="canonical",
        help="Validated representation to print.",
    )
    args = parser.parse_args(argv)

    try:
        version = parse_release_version(args.version)
    except ValueError as exc:
        parser.error(str(exc))

    sys.stdout.write(f"{getattr(version, args.format)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
