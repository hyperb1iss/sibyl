from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

PACKAGES = ("sibyl-dev", "sibyld", "sibyl-core")
_PRERELEASE_LABELS = {
    "a": "a",
    "alpha": "a",
    "b": "b",
    "beta": "b",
    "c": "rc",
    "pre": "rc",
    "preview": "rc",
    "rc": "rc",
}


@dataclass(frozen=True)
class PackageArtifact:
    name: str
    url: str
    sha256: str


def pep440_version(version: str) -> str:
    match = re.fullmatch(
        r"(?P<base>[0-9]+(?:\.[0-9]+)*)(?:-(?P<label>[A-Za-z]+)\.(?P<number>[0-9]+))?",
        version,
    )
    if not match or not match.group("label"):
        return version

    label = _PRERELEASE_LABELS.get(match.group("label").lower(), match.group("label").lower())
    return f"{match.group('base')}{label}{match.group('number')}"


def fetch_package_artifact(package: str, version: str) -> PackageArtifact:
    with urlopen(f"https://pypi.org/pypi/{package}/{version}/json", timeout=30) as response:
        payload = json.load(response)

    artifacts = payload.get("urls") or []
    candidates = [
        artifact
        for artifact in artifacts
        if artifact.get("packagetype") == "sdist" and artifact.get("digests", {}).get("sha256")
    ]
    if not candidates:
        candidates = [
            artifact
            for artifact in artifacts
            if artifact.get("python_version") == "py3" and artifact.get("digests", {}).get("sha256")
        ]
    if not candidates:
        raise RuntimeError(f"No usable PyPI artifact found for {package} {version}")

    artifact = candidates[0]
    return PackageArtifact(
        name=package,
        url=str(artifact["url"]),
        sha256=str(artifact["digests"]["sha256"]),
    )


def render_formula(
    *,
    release_version: str,
    python_version: str,
    artifacts: dict[str, PackageArtifact],
) -> str:
    cli = artifacts["sibyl-dev"]
    resources = [artifacts["sibyl-core"], artifacts["sibyld"]]
    resource_blocks = "\n\n".join(
        f'  resource "{artifact.name}" do\n'
        f'    url "{artifact.url}"\n'
        f'    sha256 "{artifact.sha256}"\n'
        "  end"
        for artifact in resources
    )

    return f'''# typed: false
# frozen_string_literal: true

class Sibyl < Formula
  include Language::Python::Virtualenv

  desc "Persistent memory and task coordination for AI coding agents"
  homepage "https://github.com/hyperb1iss/sibyl"
  url "{cli.url}"
  sha256 "{cli.sha256}"
  license "Apache-2.0"
  version "{release_version}"

  PYTHON_PACKAGE_VERSION = "{python_version}"

  depends_on "python@3.13"

{resource_blocks}

  def install
    venv = virtualenv_create(libexec, "python3.13")

    resource("sibyl-core").stage do
      venv.pip_install Pathname.pwd
    end

    resource("sibyld").stage do
      venv.pip_install Pathname.pwd
    end

    venv.pip_install buildpath
    bin.install_symlink libexec/"bin/sibyl"
    bin.install_symlink libexec/"bin/sibyld"
  end

  test do
    assert_match PYTHON_PACKAGE_VERSION, shell_output("#{{bin}}/sibyl --version")
    assert_match PYTHON_PACKAGE_VERSION, shell_output("#{{bin}}/sibyld --version")
  end
end
'''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the Homebrew formula for Sibyl.")
    parser.add_argument("--version", required=True, help="Release version, e.g. 1.0.0-rc.1")
    parser.add_argument("--output", required=True, type=Path, help="Formula path to write")
    args = parser.parse_args(argv)

    python_version = pep440_version(args.version)
    artifacts = {package: fetch_package_artifact(package, python_version) for package in PACKAGES}
    formula = render_formula(
        release_version=args.version,
        python_version=python_version,
        artifacts=artifacts,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(formula, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
