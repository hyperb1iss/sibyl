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
