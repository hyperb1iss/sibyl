"""Version stamps keep padded Markdown tables aligned.

Prettier aligns table columns, so a stamp that changes the version's length
(1.4.9 -> 1.4.10) must re-pad its cell or docs:lint fails the release gate.
"""

from __future__ import annotations

import pytest
from tools.release.sync_versions import REPO_ROOT, _apply, _targets

ANSIBLE_DOC = "docs/deployment/ansible.md"


def _stamp(text: str, version: str) -> str:
    stamped, counts = _apply(text, _targets(version)[ANSIBLE_DOC])
    assert 0 not in counts, f"{ANSIBLE_DOC} lost its sibyl_version anchor"
    return stamped


def _version_row(text: str) -> str:
    return next(line for line in text.splitlines() if "`sibyl_version`" in line)


@pytest.mark.parametrize("version", ["1.4.10", "1.5.0", "1.10.0-rc.12", "10.20.30"])
def test_stamping_keeps_the_ansible_table_row_width(version: str) -> None:
    original = (REPO_ROOT / ANSIBLE_DOC).read_text(encoding="utf-8")
    row = _version_row(original)

    stamped_row = _version_row(_stamp(original, version))

    assert f"`{version}`" in stamped_row
    assert len(stamped_row) == len(row)
    assert [i for i, c in enumerate(stamped_row) if c == "|"] == [
        i for i, c in enumerate(row) if c == "|"
    ]


def test_stamping_back_and_forth_restores_the_original_row() -> None:
    original = (REPO_ROOT / ANSIBLE_DOC).read_text(encoding="utf-8")
    current = _version_row(original).split("`")[3]

    round_trip = _stamp(_stamp(original, "1.4.10"), current)

    assert round_trip == original
