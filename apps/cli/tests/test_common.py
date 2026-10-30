"""Tests for shared CLI helpers."""

import os
import re
from pathlib import Path

import pytest

from sibyl_cli import common
from sibyl_cli.common import read_content_file


@pytest.fixture
def root_owned_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> set[Path]:
    """Stand tmp_path in for `/` and report links added to the set as root's.

    Only root can create a root-owned link or write to the real `/`, so a test
    can make neither. Ownership and the root's location are the only facts
    faked here; the CLI runs as an ordinary user.
    """
    owned: set[Path] = set()
    real_owner = common._link_owner
    monkeypatch.setattr(
        common, "_link_owner", lambda link: 0 if link in owned else real_owner(link)
    )
    monkeypatch.setattr(common, "_filesystem_root", lambda link: tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 501, raising=False)
    return owned


def _macos_root(tmp_path: Path, root_owned_links: set[Path]) -> Path:
    """Lay out macOS's /tmp and /var: root-owned links into /private."""
    for name in ("tmp", "var"):
        real = tmp_path / "private" / name
        real.mkdir(parents=True)
        link = tmp_path / name
        link.symlink_to(real, target_is_directory=True)
        root_owned_links.add(link)
    return tmp_path


def test_read_content_file_reads_a_macos_tmp_path(
    tmp_path: Path, root_owned_links: set[Path]
) -> None:
    root = _macos_root(tmp_path, root_owned_links)
    (root / "private" / "tmp" / "note.md").write_text("from /tmp", encoding="utf-8")

    assert read_content_file(str(root / "tmp" / "note.md")) == "from /tmp"


def test_read_content_file_reads_a_macos_tmpdir_path(
    tmp_path: Path, root_owned_links: set[Path]
) -> None:
    # tempfile on macOS hands out /var/folders/<xx>/<hash>/T/..., below /var.
    root = _macos_root(tmp_path, root_owned_links)
    session = root / "private" / "var" / "folders" / "l8" / "wnmp8hs1" / "T" / "tmpk3x9"
    session.mkdir(parents=True)
    (session / "body.md").write_text("from $TMPDIR", encoding="utf-8")

    tmpdir_path = root / "var" / "folders" / "l8" / "wnmp8hs1" / "T" / "tmpk3x9" / "body.md"
    assert read_content_file(str(tmpdir_path)) == "from $TMPDIR"


def test_read_content_file_rejects_a_file_link_inside_a_system_dir(
    tmp_path: Path, root_owned_links: set[Path]
) -> None:
    root = _macos_root(tmp_path, root_owned_links)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    (root / "private" / "tmp" / "note.md").symlink_to(secret)

    with pytest.raises(ValueError, match="Refusing to read symlink"):
        read_content_file(str(root / "tmp" / "note.md"))


def test_read_content_file_rejects_a_user_link_below_a_system_dir(
    tmp_path: Path, root_owned_links: set[Path]
) -> None:
    # A checkout under /tmp can still plant a directory link of its own.
    root = _macos_root(tmp_path, root_owned_links)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    checkout = root / "private" / "tmp" / "checkout"
    checkout.mkdir()
    (checkout / "docs").symlink_to(outside, target_is_directory=True)

    planted = root / "tmp" / "checkout" / "docs"
    with pytest.raises(ValueError, match=f"Refusing to read symlink.*{re.escape(str(planted))}"):
        read_content_file(str(planted / "secret.txt"))


def test_read_content_file_rejects_a_root_owned_link_inside_a_checkout(
    tmp_path: Path, root_owned_links: set[Path]
) -> None:
    # A checkout or archive unpacked with sudo carries root-owned links, so
    # below the filesystem root, ownership alone proves nothing.
    root = _macos_root(tmp_path, root_owned_links)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    checkout = root / "private" / "tmp" / "checkout"
    checkout.mkdir()
    (checkout / "docs").symlink_to(outside, target_is_directory=True)

    planted = root / "tmp" / "checkout" / "docs"
    root_owned_links.add(planted)
    with pytest.raises(ValueError, match=f"Refusing to read symlink.*{re.escape(str(planted))}"):
        read_content_file(str(planted / "secret.txt"))


def test_read_content_file_trusts_no_parent_link_when_running_as_root(
    tmp_path: Path, root_owned_links: set[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # As root, every link git or tar creates is root-owned too, so ownership
    # stops telling a system link from a planted one.
    root = _macos_root(tmp_path, root_owned_links)
    (root / "private" / "tmp" / "note.md").write_text("from /tmp", encoding="utf-8")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

    with pytest.raises(ValueError, match="Refusing to read symlink"):
        read_content_file(str(root / "tmp" / "note.md"))


def test_read_content_file_rejects_oversized_file(tmp_path: Path) -> None:
    content_file = tmp_path / "large.md"
    content_file.write_text("too large", encoding="utf-8")

    with pytest.raises(ValueError, match="too large"):
        read_content_file(str(content_file), max_size=3)


def test_read_content_file_rejects_binary_file(tmp_path: Path) -> None:
    content_file = tmp_path / "binary.bin"
    content_file.write_bytes(b"\xff\x00\xfe")

    with pytest.raises(ValueError, match="binary or non-UTF-8"):
        read_content_file(str(content_file))


def test_read_content_file_rejects_parent_symlink(tmp_path: Path) -> None:
    # A link the caller's own account created (a cloned repo, an unpacked
    # archive) is exactly what the guard exists to catch.
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="Refusing to read symlink"):
        read_content_file(str(linked_dir / "secret.txt"))


def test_read_content_file_allows_parent_symlink_when_enabled(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(outside, target_is_directory=True)

    assert read_content_file(str(linked_dir / "secret.txt"), follow_symlinks=True) == "secret"


def test_an_id_column_wraps_instead_of_cropping() -> None:
    """A cropped ID invites a guessed tail, and the guess buffers a doomed write.

    Six queued writes once referenced task UUIDs that never existed: the table
    ellipsized the ID column, and the missing characters were filled in from
    imagination. An ID has to survive the render intact.
    """
    from io import StringIO

    from rich.console import Console

    from sibyl_cli.common import create_table

    task_id = "1a156f2b-b89d-4a0f-ab5c-8e20c20cf18d"
    table = create_table(None, "ID", "Title")
    table.add_row(task_id, "Pending-writes queue holds six entries with mutated task UUIDs")
    buffer = StringIO()
    Console(file=buffer, width=60, no_color=True).print(table)

    rendered = buffer.getvalue()
    # The ID wraps onto a second line, so both halves survive in full.
    assert task_id[:26] in rendered
    assert task_id[26:] in rendered
    assert "…" not in rendered


def test_a_long_free_text_column_still_crops() -> None:
    """Only identifiers get the wrap; prose columns keep the compact render."""
    from io import StringIO

    from rich.console import Console

    from sibyl_cli.common import create_table

    table = create_table(None, "Name", "Description")
    table.add_row("entity", "z" * 400)
    buffer = StringIO()
    Console(file=buffer, width=60, no_color=True).print(table)

    assert "…" in buffer.getvalue()
