"""Contract tests for the `.sibyl/memory/` materialized projection."""

from __future__ import annotations

import json
import random
import re
import stat
from pathlib import Path

import pytest

from sibyl_core.export import (
    HANDBOOK_PATH,
    INDEX_PATH,
    MANIFEST_PATH,
    MEMORY_FILES_SCHEMA_VERSION,
    NOTES_DIR,
    README_PATH,
    RECENT_PATH,
    TASKS_PATH,
    MemoryRecord,
    MemorySnapshot,
    build_memory_files_bundle,
    memory_files,
    memory_record_from_entity,
    validate_memory_files_bundle,
    write_memory_files_bundle,
)


def _note(
    record_id: str,
    title: str,
    entity_type: str = "decision",
    *,
    body: str | None = None,
    updated_at: str = "2026-07-20T10:00:00Z",
    tags: tuple[str, ...] = (),
    attributes: tuple[tuple[str, str], ...] = (),
) -> MemoryRecord:
    return MemoryRecord(
        id=record_id,
        entity_type=entity_type,
        title=title,
        body=f"Body for {title}." if body is None else body,
        updated_at=updated_at,
        project_id="project_demo",
        tags=tags,
        attributes=attributes,
    )


def _snapshot(**overrides: object) -> MemorySnapshot:
    base: dict[str, object] = {
        "project_id": "project_demo",
        "project_name": "Demo Project",
        "project_description": "A project that keeps its memory in the repo.",
        "notes": (
            _note("decision_beta", "Namespace per organization"),
            _note(
                "pattern_alpha",
                "Pool Surreal connections per org",
                entity_type="pattern",
                updated_at="2026-07-22T09:30:00Z",
                tags=("surreal", "concurrency"),
            ),
        ),
        "tasks": (
            _note(
                "task_gamma",
                "Materialize memory as files",
                entity_type="task",
                updated_at="2026-07-24T08:00:00Z",
                attributes=(("status", "doing"), ("priority", "high")),
            ),
        ),
    }
    base.update(overrides)
    return MemorySnapshot(**base)  # type: ignore[arg-type]


def _written_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_bundle_carries_the_documented_layout() -> None:
    bundle = build_memory_files_bundle(_snapshot())

    assert README_PATH in bundle.files
    assert INDEX_PATH in bundle.files
    assert RECENT_PATH in bundle.files
    assert TASKS_PATH in bundle.files
    assert MANIFEST_PATH in bundle.files
    assert HANDBOOK_PATH not in bundle.files

    note_paths = [path for path in bundle.files if path.startswith(f"{NOTES_DIR}/")]
    assert note_paths == [
        f"{NOTES_DIR}/decision-namespace-per-organization.md",
        f"{NOTES_DIR}/pattern-pool-surreal-connections-per-org.md",
    ]
    assert list(bundle.files) == sorted(bundle.files)


def test_every_markdown_file_declares_an_okf_type() -> None:
    bundle = build_memory_files_bundle(_snapshot(handbook="# Handbook\n\nStart here."))

    markdown = {path: content for path, content in bundle.files.items() if path.endswith(".md")}
    assert markdown
    for path, content in markdown.items():
        assert content.startswith("---\n"), path
        frontmatter = content[len("---\n") : content.find("\n---\n", len("---\n"))]
        types = [line for line in frontmatter.splitlines() if line.startswith("type: ")]
        assert types == [types[0]] and types[0] != 'type: ""', path


def test_handbook_is_a_reserved_slot_that_fills_in_when_supplied() -> None:
    without = build_memory_files_bundle(_snapshot())
    with_handbook = build_memory_files_bundle(
        _snapshot(handbook="# Demo handbook\n\nThe project is about memory.")
    )

    assert HANDBOOK_PATH not in without.files
    assert HANDBOOK_PATH in with_handbook.files
    assert "The project is about memory." in with_handbook.files[HANDBOOK_PATH]
    assert f"./{HANDBOOK_PATH}" in with_handbook.files[INDEX_PATH]
    assert f"./{HANDBOOK_PATH}" not in without.files[INDEX_PATH]
    assert json.loads(with_handbook.files[MANIFEST_PATH])["handbook"] is True
    assert json.loads(without.files[MANIFEST_PATH])["handbook"] is False


def test_rebuilding_an_unchanged_snapshot_is_byte_identical() -> None:
    snapshot = _snapshot()

    assert build_memory_files_bundle(snapshot).files == build_memory_files_bundle(snapshot).files


def test_input_ordering_does_not_change_a_single_byte() -> None:
    snapshot = _snapshot()
    canonical = build_memory_files_bundle(snapshot)

    shuffler = random.Random(20260725)
    for _ in range(8):
        notes = list(snapshot.notes)
        tasks = list(snapshot.tasks)
        shuffler.shuffle(notes)
        shuffler.shuffle(tasks)
        reordered = build_memory_files_bundle(
            MemorySnapshot(
                project_id=snapshot.project_id,
                project_name=snapshot.project_name,
                project_description=snapshot.project_description,
                notes=tuple(notes),
                tasks=tuple(tasks),
            )
        )
        assert reordered.files == canonical.files


def test_rematerializing_produces_identical_bytes_on_disk(tmp_path: Path) -> None:
    bundle = build_memory_files_bundle(_snapshot())
    root = tmp_path / ".sibyl" / "memory"

    write_memory_files_bundle(bundle, root)
    first = _written_bytes(root)
    write_memory_files_bundle(build_memory_files_bundle(_snapshot()), root)
    second = _written_bytes(root)

    assert first == second
    assert validate_memory_files_bundle(root) == []


def test_no_wall_clock_value_leaks_into_the_projection() -> None:
    # The projection is a pure function of the snapshot, so a build taken at any
    # later moment has to match. Anything sampled from `datetime.now` breaks this.
    snapshot = _snapshot()
    first = build_memory_files_bundle(snapshot)
    second = build_memory_files_bundle(snapshot)

    assert first.content_digest == second.content_digest
    assert "generated_at" not in first.files[MANIFEST_PATH]
    assert "exported_at" not in first.files[MANIFEST_PATH]


def test_the_projection_module_cannot_reach_a_clock() -> None:
    """Comparing two same-process builds misses a date-only leak.

    `date.today()` returns the same value twice in a row, so the digest check
    above passes while the files churn at midnight. The decisive property is
    that the module has no clock to call in the first place.
    """
    source = Path(memory_files.__file__).read_text(encoding="utf-8")
    imports = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    ]

    assert imports
    for line in imports:
        assert not re.search(r"\b(datetime|time|calendar|random|uuid|os)\b", line), line


def test_manifest_digests_match_the_written_files(tmp_path: Path) -> None:
    bundle = build_memory_files_bundle(_snapshot())
    root = tmp_path / "memory"
    write_memory_files_bundle(bundle, root)

    manifest = json.loads((root / MANIFEST_PATH).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == MEMORY_FILES_SCHEMA_VERSION
    assert manifest["project"]["id"] == "project_demo"
    assert manifest["counts"] == {"notes": 2, "tasks": 1, "files": len(bundle.files)}

    listed = {entry["path"] for entry in manifest["files"]}
    assert listed == set(bundle.files) - {MANIFEST_PATH}
    assert validate_memory_files_bundle(root) == []


def test_validation_reports_content_edited_after_export(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    write_memory_files_bundle(build_memory_files_bundle(_snapshot()), root)

    index = root / INDEX_PATH
    index.chmod(0o644)
    index.write_text(index.read_text(encoding="utf-8") + "\nhand edit\n", encoding="utf-8")

    errors = validate_memory_files_bundle(root)
    assert errors == [f"{INDEX_PATH}: content does not match the manifest digest"]


def test_files_are_written_read_only_by_default(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    write_memory_files_bundle(build_memory_files_bundle(_snapshot()), root)

    mode = stat.S_IMODE((root / INDEX_PATH).stat().st_mode)
    assert mode == 0o444

    write_memory_files_bundle(build_memory_files_bundle(_snapshot()), root, read_only=False)
    assert stat.S_IMODE((root / INDEX_PATH).stat().st_mode) == 0o644


def test_stale_notes_are_pruned_and_foreign_files_survive(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    write_memory_files_bundle(build_memory_files_bundle(_snapshot()), root)

    # A file this exporter never wrote is not ours to delete, wherever it sits.
    keepers = {
        root / "OPERATOR_NOTES.md": "hand written, not ours\n",
        root / NOTES_DIR / "MY_OWN_NOTE.md": "also not ours\n",
        root / NOTES_DIR / "sub" / "deep.md": "nested, still not ours\n",
    }
    for path, content in keepers.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    dropped = root / NOTES_DIR / "decision-namespace-per-organization.md"
    assert dropped.is_file()

    trimmed = _snapshot(
        notes=(_note("pattern_alpha", "Pool Surreal connections per org", entity_type="pattern"),)
    )
    write_memory_files_bundle(build_memory_files_bundle(trimmed), root)

    assert not dropped.exists()
    for path, content in keepers.items():
        assert path.read_text(encoding="utf-8") == content, path
    assert validate_memory_files_bundle(root) == []


def test_writing_into_a_populated_foreign_directory_is_refused(tmp_path: Path) -> None:
    """`--output .` in a repo root would otherwise eat that repo's README."""
    root = tmp_path / "someones-repo"
    root.mkdir()
    readme = root / "README.md"
    readme.write_text("# Their project\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        write_memory_files_bundle(build_memory_files_bundle(_snapshot()), root)
    assert readme.read_text(encoding="utf-8") == "# Their project\n"

    write_memory_files_bundle(build_memory_files_bundle(_snapshot()), root, force=True)
    assert validate_memory_files_bundle(root) == []


def test_an_empty_or_missing_directory_needs_no_force(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    write_memory_files_bundle(build_memory_files_bundle(_snapshot()), empty)
    assert validate_memory_files_bundle(empty) == []

    fresh = tmp_path / "nested" / "fresh"
    write_memory_files_bundle(build_memory_files_bundle(_snapshot()), fresh)
    assert validate_memory_files_bundle(fresh) == []


def test_a_long_title_still_produces_a_writable_filename(tmp_path: Path) -> None:
    long_title = "Materialize " + "extraordinarily verbose memory " * 20
    bundle = build_memory_files_bundle(
        _snapshot(
            notes=(_note("decision_long", long_title), _note("decision_long_two", long_title))
        )
    )

    note_paths = [path for path in bundle.files if path.startswith(f"{NOTES_DIR}/")]
    assert len(note_paths) == 2
    for path in note_paths:
        assert len(Path(path).name) < 255, path

    root = tmp_path / "memory"
    write_memory_files_bundle(bundle, root)
    assert validate_memory_files_bundle(root) == []


def test_a_body_containing_a_frontmatter_delimiter_stays_parseable(tmp_path: Path) -> None:
    body = "Intro paragraph.\n\n---\n\nSection after a horizontal rule."
    root = tmp_path / "memory"
    write_memory_files_bundle(
        build_memory_files_bundle(_snapshot(notes=(_note("decision_rule", "Ruled", body=body),))),
        root,
    )

    note = (root / NOTES_DIR / "decision-ruled.md").read_text(encoding="utf-8")
    assert note.startswith('---\ntype: "Sibyl Decision"')
    assert "Section after a horizontal rule." in note
    assert validate_memory_files_bundle(root) == []


def test_colliding_titles_both_disambiguate_regardless_of_order() -> None:
    left = _note("decision_one", "Same title")
    right = _note("decision_two", "Same title")

    forward = build_memory_files_bundle(_snapshot(notes=(left, right)))
    backward = build_memory_files_bundle(_snapshot(notes=(right, left)))

    assert forward.files == backward.files
    assert f"{NOTES_DIR}/decision-same-title.md" not in forward.files
    assert f"{NOTES_DIR}/decision-same-title-decision_one.md" in forward.files
    assert f"{NOTES_DIR}/decision-same-title-decision_two.md" in forward.files


def test_ids_and_bodies_are_greppable_from_the_index_and_notes() -> None:
    bundle = build_memory_files_bundle(_snapshot())

    assert "`pattern_alpha`" in bundle.files[INDEX_PATH]
    assert "`task_gamma`" in bundle.files[TASKS_PATH]
    note = bundle.files[f"{NOTES_DIR}/pattern-pool-surreal-connections-per-org.md"]
    assert 'sibyl_id: "pattern_alpha"' in note
    assert "Body for Pool Surreal connections per org." in note
    assert "sibyl cite pattern_alpha" in note


def test_recent_orders_by_recency_then_id() -> None:
    older = _note("decision_z", "Older", updated_at="2026-07-01T00:00:00Z")
    newer_a = _note("decision_a", "Newer A", updated_at="2026-07-09T00:00:00Z")
    newer_b = _note("decision_b", "Newer B", updated_at="2026-07-09T00:00:00Z")

    bundle = build_memory_files_bundle(_snapshot(notes=(older, newer_b, newer_a)))
    recent = bundle.files[RECENT_PATH]

    assert recent.index("Newer A") < recent.index("Newer B") < recent.index("Older")
    # Reached through the builder the input is already id-sorted, so drive the
    # tiebreak directly: equal timestamps must resolve by id, not arrival order.
    ordered = memory_files._by_recency([newer_b, older, newer_a])
    assert [record.id for record in ordered] == ["decision_a", "decision_b", "decision_z"]


def test_note_files_carry_the_whole_body_not_a_preview() -> None:
    body = "\n\n".join(f"Paragraph {index} of a memory that runs long." for index in range(40))
    bundle = build_memory_files_bundle(_snapshot(notes=(_note("decision_big", "Big", body=body),)))

    note = bundle.files[f"{NOTES_DIR}/decision-big.md"]
    assert len(body) > 1000
    assert body in note
    assert "Paragraph 39 of a memory that runs long." in note


@pytest.mark.parametrize(
    "volatile_field",
    ["retrieval_count", "last_recalled_at", "citation_count", "misled_count", "record_id"],
)
def test_volatile_bookkeeping_never_reaches_the_files(volatile_field: str) -> None:
    payload = {
        "id": "decision_live",
        "type": "decision",
        "name": "Live entity",
        "description": "truncated preview",
        "metadata": {
            "description": "The full decision body lives in metadata.",
            "updated_at": "2026-07-20T10:00:00Z",
            "project_id": "project_demo",
            "tags": ["surreal"],
            volatile_field: "volatile-marker",
            "embedding_metadata": {"model": "text-embedding-3-small"},
        },
    }
    record = memory_record_from_entity(payload)
    bundle = build_memory_files_bundle(_snapshot(notes=(record,), tasks=()))

    assert record.body == "The full decision body lives in metadata."
    assert record.tags == ("surreal",)
    for content in bundle.files.values():
        assert volatile_field not in content
        assert "volatile-marker" not in content
        assert "embedding_metadata" not in content


def test_write_refuses_a_non_directory_target(tmp_path: Path) -> None:
    blocker = tmp_path / "memory"
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_memory_files_bundle(build_memory_files_bundle(_snapshot()), blocker)


def test_validation_reports_a_directory_that_was_never_exported(tmp_path: Path) -> None:
    empty = tmp_path / "memory"
    empty.mkdir()

    assert validate_memory_files_bundle(empty) == [f"missing {MANIFEST_PATH}"]
