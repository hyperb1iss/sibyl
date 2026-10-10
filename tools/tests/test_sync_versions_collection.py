"""Version stamps reach the Ansible collection metadata and its install example.

Deployment repos pin the collection by release tag, so the README example and
galaxy.yml must name the release being cut, never the one before it.
"""

from __future__ import annotations

import pytest
from tools.release.sync_versions import REPO_ROOT, _apply, _targets

COLLECTION_FILES = ("infra/ansible/galaxy.yml", "infra/ansible/README.md")


@pytest.mark.parametrize("rel_path", COLLECTION_FILES)
@pytest.mark.parametrize("version", ["1.4.10", "1.10.0-rc.12"])
def test_stamping_pins_the_collection_to_the_release(rel_path: str, version: str) -> None:
    original = (REPO_ROOT / rel_path).read_text(encoding="utf-8")

    stamped, counts = _apply(original, _targets(version)[rel_path])

    assert 0 not in counts, f"{rel_path} lost its version anchor"
    expected = f"version: v{version}" if rel_path.endswith("README.md") else f"version: {version}"
    assert expected in stamped
    assert stamped.count(version) == 1


def test_stamping_pins_the_deployment_guide_install_example() -> None:
    rel_path = "docs/deployment/ansible.md"
    original = (REPO_ROOT / rel_path).read_text(encoding="utf-8")

    stamped, counts = _apply(original, _targets("1.4.10")[rel_path])

    assert 0 not in counts, f"{rel_path} lost a version anchor"
    assert "      version: v1.4.10\n" in stamped
