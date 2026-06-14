from __future__ import annotations

from pathlib import Path

import pytest

from sibyl.crawler.local import LocalFileCrawler


def test_local_file_crawler_rejects_paths_outside_source_import_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_root = tmp_path / "imports"
    outside = tmp_path / "outside"
    import_root.mkdir()
    outside.mkdir()
    monkeypatch.setattr("sibyl.crawler.local.settings.source_import_dir", import_root)

    with pytest.raises(ValueError, match="outside source import directory"):
        LocalFileCrawler()._parse_path(str(outside))


def test_local_file_crawler_allows_paths_inside_source_import_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_root = tmp_path / "imports"
    source_dir = import_root / "docs"
    source_dir.mkdir(parents=True)
    monkeypatch.setattr("sibyl.crawler.local.settings.source_import_dir", import_root)

    assert LocalFileCrawler()._parse_path(str(source_dir)) == source_dir.resolve()


def test_local_file_crawler_rejects_symlinks_inside_source_import_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_root = tmp_path / "imports"
    source_dir = import_root / "docs"
    outside_file = tmp_path / "secret.md"
    source_dir.mkdir(parents=True)
    outside_file.write_text("secret", encoding="utf-8")
    (source_dir / "secret.md").symlink_to(outside_file)
    monkeypatch.setattr("sibyl.crawler.local.settings.source_import_dir", import_root)

    with pytest.raises(ValueError, match="includes a symlink"):
        LocalFileCrawler()._parse_path(str(source_dir))
