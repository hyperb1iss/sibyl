from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.release.homebrew_formula import PackageArtifact, fetch_package_artifact, pep440_version

PACKAGES = ("sibyl-dev", "sibyl-core")


def render_pkgbuild(
    *,
    python_version: str,
    artifacts: dict[str, PackageArtifact],
) -> str:
    cli = artifacts["sibyl-dev"]
    core = artifacts["sibyl-core"]

    return f"""# Maintainer: Stefanie Jane <stef@hyperbliss.tech>

pkgname=sibyl
pkgver={python_version}
pkgrel=1
pkgdesc="Persistent memory and task coordination CLI for AI coding agents"
arch=('any')
url="https://github.com/hyperb1iss/sibyl"
license=('Apache-2.0')
provides=('sibyl-cli')
conflicts=('sibyl-cli')
depends=(
    'docker'
    'docker-compose'
    'python>=3.13'
    'python-anyio'
    'python-dotenv'
    'python-httpx'
    'python-passlib'
    'python-pydantic'
    'python-pydantic-settings'
    'python-pyjwt'
    'python-yaml'
    'python-rich'
    'python-structlog'
    'python-tomli-w'
    'python-typer'
    'python-websockets'
)
makedepends=(
    'python-build'
    'python-hatchling'
    'python-installer'
    'python-wheel'
)
source=(
    "sibyl-dev-${{pkgver}}.tar.gz::{cli.url}"
    "sibyl-core-${{pkgver}}.tar.gz::{core.url}"
)
sha256sums=(
    '{cli.sha256}'
    '{core.sha256}'
)

build() {{
    python -m build --wheel --no-isolation "sibyl_core-${{pkgver}}"
    python -m build --wheel --no-isolation "sibyl_dev-${{pkgver}}"
}}

package() {{
    python -m installer --destdir="${{pkgdir}}" "sibyl_core-${{pkgver}}"/dist/*.whl
    python -m installer --destdir="${{pkgdir}}" "sibyl_dev-${{pkgver}}"/dist/*.whl
    install -Dm644 "sibyl_dev-${{pkgver}}/README.md" "${{pkgdir}}/usr/share/doc/${{pkgname}}/README.md"
}}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the AUR PKGBUILD for Sibyl.")
    parser.add_argument("--version", required=True, help="Release version, e.g. 1.0.0-rc.1")
    parser.add_argument("--output", required=True, type=Path, help="PKGBUILD path to write")
    args = parser.parse_args(argv)

    python_version = pep440_version(args.version)
    artifacts = {package: fetch_package_artifact(package, python_version) for package in PACKAGES}
    pkgbuild = render_pkgbuild(
        python_version=python_version,
        artifacts=artifacts,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(pkgbuild, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
