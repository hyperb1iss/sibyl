from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from shutil import which
from typing import NotRequired, TypedDict, cast

import pytest
from tools.trust import large_corpus_rehearsal

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_DUPLICATE_RECORDS = 2
EXPECTED_IMPORTED_COUNT = 57
EXPECTED_SKIPPED_COUNT = 3
EXPECTED_DEDUPE_COUNT = 1
EXPECTED_ERROR_COUNT = 1


class MoonTask(TypedDict):
    command: str
    args: NotRequired[list[str]]
    target: str


class MoonTaskQuery(TypedDict):
    tasks: dict[str, dict[str, MoonTask]]


def _root_moon_tasks() -> dict[str, MoonTask]:
    moon = which("moon")
    assert moon is not None

    result = subprocess.run(  # noqa: S603
        [moon, "query", "tasks", "--project", "root"],
        cwd=REPO_ROOT,
        env={**os.environ, "MOON_COLOR": "false"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = cast(MoonTaskQuery, json.loads(result.stdout))
    return payload["tasks"]["root"]


def test_fixture_declares_required_large_corpus_cases() -> None:
    fixture = large_corpus_rehearsal.load_fixture()
    private_entries = large_corpus_rehearsal._private_entries(fixture)
    project_entries = large_corpus_rehearsal._project_entries(fixture)

    assert len(private_entries) > len(project_entries)
    assert any(entry.case == "metadata_only" for entry in private_entries)
    assert any(entry.attachments for entry in private_entries)
    duplicate_count = sum(
        entry.adapter_record_id == "private-duplicate-stable" for entry in private_entries
    )
    assert duplicate_count == EXPECTED_DUPLICATE_RECORDS
    assert any(entry.skip_reason == "fixture_skipped" for entry in private_entries)
    assert any(entry.skip_reason == "fixture_parse_error" for entry in private_entries)
    assert {entry.privacy_class.value for entry in project_entries} == {"project"}


def test_rehearsal_writes_release_citable_receipt(tmp_path: Path) -> None:
    messages: list[str] = []
    artifact_path = tmp_path / "receipt.json"

    receipt = large_corpus_rehearsal.run_rehearsal(
        artifact_path=artifact_path,
        echo=messages.append,
    )

    written = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "PASS"
    assert written == receipt
    assert receipt["artifact_path"] == str(artifact_path)
    assert receipt["totals"]["imported_count"] == EXPECTED_IMPORTED_COUNT
    assert receipt["totals"]["skipped_count"] == EXPECTED_SKIPPED_COUNT
    assert receipt["totals"]["dedupe_count"] == EXPECTED_DEDUPE_COUNT
    assert receipt["workspace_progress_contract"]["error_count"] == EXPECTED_ERROR_COUNT
    assert receipt["checks"]["scope_leak"]["status"] == "PASS"
    assert receipt["checks"]["policy_failure_probe"]["writes_blocked"] is True
    assert {check["name"] for check in receipt["checks"]["early_search"]} == {
        "metadata-only-before-extraction",
        "attachment-before-extraction",
    }
    assert messages[0] == "Large Corpus Rehearsal Receipt"
    assert "57 imported" in messages[-1]


def test_rehearsal_rejects_count_drift(tmp_path: Path) -> None:
    fixture = large_corpus_rehearsal.load_fixture()
    private = cast(dict[str, object], fixture["private"])
    expected = cast(dict[str, object], private["expected"])
    expected["imported_count"] = 999
    fixture_path = tmp_path / "dogfood.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(
        large_corpus_rehearsal.RehearsalFailure,
        match="private imported_count drift",
    ):
        large_corpus_rehearsal.run_rehearsal(
            fixture_path=fixture_path,
            artifact_path=tmp_path / "receipt.json",
            echo=lambda _: None,
        )


def test_main_returns_nonzero_for_drifted_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = large_corpus_rehearsal.load_fixture()
    expected = cast(dict[str, object], fixture["expected"])
    expected["raw_memory_count"] = 1
    fixture_path = tmp_path / "dogfood.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    exit_code = large_corpus_rehearsal.main(
        [
            "--fixture-path",
            str(fixture_path),
            "--artifact-path",
            str(tmp_path / "receipt.json"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "status: FAIL" in captured.out
    assert "total raw_memory_count drift" in captured.out


def test_root_moon_tasks_expose_large_corpus_rehearsal() -> None:
    tasks = _root_moon_tasks()

    gate = tasks["large-corpus-rehearsal"]
    assert gate["target"] == "root:large-corpus-rehearsal"
    assert gate["command"] == "uv"
    assert gate["args"] == ["run", "python", "-m", "tools.trust.large_corpus_rehearsal"]

    test_task = tasks["large-corpus-rehearsal-test"]
    assert test_task["target"] == "root:large-corpus-rehearsal-test"
    assert test_task["command"] == "uv"
    assert test_task["args"] == [
        "run",
        "pytest",
        "tools/tests/test_large_corpus_rehearsal.py",
        "-v",
    ]
