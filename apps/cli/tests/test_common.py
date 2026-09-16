"""Tests for shared CLI helpers."""

from pathlib import Path

import pytest

from sibyl_cli.common import read_content_file


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
