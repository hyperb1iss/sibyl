from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.release.version import pep440_version

#: The formula installs the CLI. sibyld runs as a container image, and its
#: dependency tree (scipy, playwright, crawl4ai) is not something Homebrew
#: should build on a laptop.
PACKAGES = ("sibyl-dev", "sibyl-core")

ROOT = Path(__file__).resolve().parents[2]
_PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==\S+")


@dataclass(frozen=True)
class PackageArtifact:
    name: str
    url: str
    sha256: str


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


def cli_requirements(root: Path = ROOT) -> str:
    """The CLI's third-party dependencies, pinned with hashes from uv.lock.

    Homebrew's ``pip_install`` passes ``--no-deps``, so a formula that only
    stages Sibyl's own packages installs none of what they import. These
    pins come from the lock at the release tag, every one carrying the
    hashes pip checks under ``--require-hashes``.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to export the CLI's pinned requirements")
    exported = subprocess.run(  # noqa: S603
        [
            uv,
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-workspace",
            "--no-header",
            "--no-annotate",
            "--package",
            "sibyl-dev",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    validate_requirements(exported)
    return exported


def validate_requirements(requirements: str) -> None:
    """Refuse requirements pip could not install under ``--require-hashes``."""
    entries = [entry.strip() for entry in requirements.replace("\\\n", " ").splitlines()]
    entries = [entry for entry in entries if entry and not entry.startswith("#")]
    if not entries:
        raise RuntimeError("uv export produced no CLI requirements")
    for entry in entries:
        if not _PIN.match(entry):
            raise RuntimeError(f"CLI requirement is not pinned to one version: {entry}")
        if "--hash=sha256:" not in entry:
            raise RuntimeError(f"CLI requirement has no hash: {entry}")
        if entry.split("==", 1)[0].lower() in PACKAGES:
            raise RuntimeError(f"CLI requirements must not include Sibyl's own {entry}")


def render_formula(
    *,
    release_version: str,
    python_version: str,
    artifacts: dict[str, PackageArtifact],
    requirements: str,
) -> str:
    validate_requirements(requirements)
    cli = artifacts["sibyl-dev"]
    core = artifacts["sibyl-core"]
    pinned = "".join(
        f"    {line}\n" if line else "\n" for line in requirements.strip().splitlines()
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

  # The CLI's third-party dependencies, pinned with hashes from the release's
  # uv.lock. They install as wheels, so no compiler or Rust toolchain is needed.
  REQUIREMENTS = <<~'EOS'
{pinned}  EOS

  depends_on "python@3.13"

  resource "sibyl-core" do
    url "{core.url}"
    sha256 "{core.sha256}"
  end

  def install
    venv = virtualenv_create(libexec, "python3.13")

    # Homebrew's pip_install skips dependencies, so they install here first.
    (buildpath/"requirements.txt").write REQUIREMENTS
    system Formula["python@3.13"].opt_bin/"python3.13", "-m", "pip",
           "--python=#{{libexec}}/bin/python", "install",
           "--require-hashes", "--no-deps", "--only-binary=:all:", "--no-cache-dir",
           "--requirement", buildpath/"requirements.txt"

    resource("sibyl-core").stage do
      venv.pip_install Pathname.pwd
    end

    venv.pip_install buildpath
    bin.install_symlink libexec/"bin/sibyl"
  end

  test do
    assert_match PYTHON_PACKAGE_VERSION, shell_output("#{{bin}}/sibyl --version")
    system Formula["python@3.13"].opt_bin/"python3.13", "-m", "pip",
           "--python=#{{libexec}}/bin/python", "check"
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
        requirements=cli_requirements(),
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(formula, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
