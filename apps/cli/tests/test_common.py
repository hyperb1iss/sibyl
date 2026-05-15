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
