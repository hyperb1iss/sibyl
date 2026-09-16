"""Unit cover for the screen48 devbox staging helpers.

Three modules are under test and one phase. ``source_manifest`` and
``runtime_manifest`` each mirror a verifier that lives elsewhere, so every test
here checks the mirror against the original rather than against itself:
``runtime_manifest.write`` hands its output straight to ``runtime_pin.verify``,
and the differential test at the bottom walks a deliberately awkward tree with
both ``source_manifest.inventory`` and the loop transcribed out of
``CurrentOwners.__call__``, asserting the two agree. ``host_binding`` is checked
against ``checkpoints.OWNER_KEYS``. The ``preflight`` phase is checked for the
property that makes it safe to run on a live eval host, that it never touches
the owned container, and for the one that makes it useful, that a staging
failure lands as a named check rather than a traceback.

No devbox, no database, no network.
"""

# Expected file counts, permission bits and exit codes are the assertions here.
# ruff: noqa: PLR2004

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from benchmarks.agent_tasks.screen48 import checkpoints
from benchmarks.agent_tasks.screen48.devbox import (
    host_binding,
    owned_db,
    run_phase,
    runtime_manifest,
    source_manifest,
)
from benchmarks.agent_tasks.screen48.recall.owners import runtime_pin


@pytest.fixture
def root_dir(tmp_path: Path) -> Path:
    """A real path with no symlink components.

    ``runtime_pin.verify`` compares resolved symlink targets against the
    unresolved runtime root, so a tmp directory reached through a symlink (the
    macOS ``/var`` case) would fail the real verifier for a reason that has
    nothing to do with what is under test.
    """
    return tmp_path.resolve()


ORGANIZATION_ID = "b60d61fd-d388-4cb0-9581-eca4f583544b"
PRINCIPAL_ID = "91bd4035-be71-4e14-a43f-c861fc75699c"
COMMIT = "a" * 40


def make_source(root: Path) -> Path:
    """A miniature clean export: nested files plus one in-tree symlink."""
    source = root / "source"
    (source / "apps" / "api").mkdir(parents=True)
    (source / "CLAUDE.md").write_text("house rules\n", encoding="utf-8")
    (source / "apps" / "api" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (source / "AGENTS.md").symlink_to("CLAUDE.md")
    return source


def make_runtime(root: Path) -> tuple[Path, Path]:
    """A miniature runtime: a base interpreter inside the root and a venv link."""
    runtime = root / "runtime"
    (runtime / "python" / "bin").mkdir(parents=True)
    base = runtime / "python" / "bin" / "python3.13"
    base.write_text("#!/bin/false\n", encoding="utf-8")
    base.chmod(0o755)
    (runtime / ".venv" / "bin").mkdir(parents=True)
    interpreter = runtime / ".venv" / "bin" / "python"
    interpreter.symlink_to(base)
    (runtime / ".venv" / "lib" / "site-packages").mkdir(parents=True)
    (runtime / ".venv" / "lib" / "site-packages" / "dep.py").write_text("y = 2\n", encoding="utf-8")
    return runtime, interpreter


def stage_a_root(root: Path) -> dict[str, Any]:
    """Everything ``stage.sh`` leaves behind, built the way ``stage.sh`` builds it."""
    source = make_source(root)
    runtime, interpreter = make_runtime(root)
    written = source_manifest.write(source, COMMIT, root / "source-manifest.json")
    pinned = runtime_manifest.write(runtime, interpreter)
    receipt = {
        "schema": "sibyl-screen48-stage-v2",
        "runtime_root": str(root),
        "source_commit": COMMIT,
        "source_manifest_sha256": written["manifest_sha256"],
        "dependency_runtime": pinned["dependency_runtime"],
    }
    (root / "stage.json").write_text(json.dumps(receipt), encoding="utf-8")
    return receipt


# ---------------------------------------------------------------------------
# Source manifest
# ---------------------------------------------------------------------------


def test_the_manifest_carries_what_the_owner_qualification_reads(root_dir: Path) -> None:
    source = make_source(root_dir)

    written = source_manifest.write(source, COMMIT, root_dir / "source-manifest.json")

    manifest = json.loads((root_dir / "source-manifest.json").read_bytes())
    assert manifest["base_commit"] == COMMIT
    assert set(manifest["files"]) == {"CLAUDE.md", "apps/api/main.py"}
    assert manifest["symlinks"] == {"AGENTS.md": "CLAUDE.md"}
    assert manifest["files"]["CLAUDE.md"] == hashlib.sha256(b"house rules\n").hexdigest()
    assert written["files"] == 2
    assert written["symlinks"] == 1


def test_the_recorded_digest_is_the_digest_of_the_written_bytes(root_dir: Path) -> None:
    source = make_source(root_dir)
    out = root_dir / "source-manifest.json"

    written = source_manifest.write(source, COMMIT, out)

    assert written["manifest_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()


def test_verify_accepts_the_tree_it_described(root_dir: Path) -> None:
    source = make_source(root_dir)
    out = root_dir / "source-manifest.json"
    written = source_manifest.write(source, COMMIT, out)

    receipt = source_manifest.verify(
        source, out, expected_sha256=written["manifest_sha256"], base_commit=COMMIT
    )

    assert receipt["status"] == "source_manifest_verified"
    assert receipt["files"] == 2


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda s: (s / "apps" / "api" / "main.py").write_text("x = 2\n"), "file inventory"),
        (lambda s: (s / "extra.py").write_text("z = 3\n"), "file inventory"),
        (lambda s: (s / "apps" / "api" / "main.py").unlink(), "file inventory"),
        (lambda s: (s / "apps" / "api" / "main.py").chmod(0o755), "file modes"),
    ],
)
def test_verify_refuses_a_tree_that_moved(root_dir: Path, mutate: Any, message: str) -> None:
    source = make_source(root_dir)
    out = root_dir / "source-manifest.json"
    source_manifest.write(source, COMMIT, out)

    mutate(source)

    with pytest.raises(source_manifest.SourceManifestError, match=message):
        source_manifest.verify(source, out)


def test_verify_refuses_a_manifest_whose_bytes_changed(root_dir: Path) -> None:
    source = make_source(root_dir)
    out = root_dir / "source-manifest.json"
    source_manifest.write(source, COMMIT, out)

    with pytest.raises(source_manifest.SourceManifestError, match="manifest changed"):
        source_manifest.verify(source, out, expected_sha256="0" * 64)


def test_verify_refuses_a_manifest_for_another_commit(root_dir: Path) -> None:
    source = make_source(root_dir)
    out = root_dir / "source-manifest.json"
    source_manifest.write(source, COMMIT, out)

    with pytest.raises(source_manifest.SourceManifestError, match="another base commit"):
        source_manifest.verify(source, out, base_commit="b" * 40)


@pytest.mark.parametrize("stray", [".git", ".venv", "__pycache__"])
def test_an_unclean_export_is_refused(root_dir: Path, stray: str) -> None:
    source = make_source(root_dir)
    (source / "apps" / stray).mkdir()

    with pytest.raises(source_manifest.SourceManifestError, match="not a clean export"):
        source_manifest.write(source, COMMIT, root_dir / "source-manifest.json")


def test_the_manifest_may_not_live_inside_the_tree_it_describes(root_dir: Path) -> None:
    source = make_source(root_dir)

    with pytest.raises(source_manifest.SourceManifestError, match="inside the source tree"):
        source_manifest.write(source, COMMIT, source / "source-manifest.json")


def test_a_symlink_out_of_the_tree_is_refused(root_dir: Path) -> None:
    source = make_source(root_dir)
    outside = root_dir / "outside.md"
    outside.write_text("elsewhere\n", encoding="utf-8")
    (source / "escape.md").symlink_to(outside)

    with pytest.raises(source_manifest.SourceManifestError, match="escapes the tree"):
        source_manifest.write(source, COMMIT, root_dir / "source-manifest.json")


def test_the_manifest_is_written_exclusively(root_dir: Path) -> None:
    source = make_source(root_dir)
    out = root_dir / "source-manifest.json"
    source_manifest.write(source, COMMIT, out)

    with pytest.raises(FileExistsError):
        source_manifest.write(source, COMMIT, out)


def test_the_source_manifest_cli_round_trips(root_dir: Path, capsys: Any) -> None:
    source = make_source(root_dir)
    out = root_dir / "source-manifest.json"

    assert (
        source_manifest.main(
            ["write", "--source", str(source), "--commit", COMMIT, "--out", str(out)]
        )
        == 0
    )
    written = json.loads(capsys.readouterr().out)
    assert (
        source_manifest.main(
            [
                "verify",
                "--source",
                str(source),
                "--manifest",
                str(out),
                "--sha256",
                written["manifest_sha256"],
                "--commit",
                COMMIT,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "source_manifest_verified"


# ---------------------------------------------------------------------------
# Runtime manifest
# ---------------------------------------------------------------------------


def test_the_written_runtime_manifest_satisfies_the_real_pin(root_dir: Path) -> None:
    runtime, interpreter = make_runtime(root_dir)

    receipt = runtime_manifest.write(runtime, interpreter)

    pinned = runtime_pin.verify(receipt["dependency_runtime"])
    assert pinned["interpreter"] == str(interpreter)
    assert pinned["files"] == receipt["files"]
    assert set(receipt["dependency_runtime"]) == {"root", "manifest_sha256", "interpreter"}


def test_the_runtime_manifest_records_digests_modes_and_links(root_dir: Path) -> None:
    runtime, interpreter = make_runtime(root_dir)

    runtime_manifest.write(runtime, interpreter)

    manifest = json.loads((runtime / "runtime-manifest.json").read_bytes())
    assert manifest["root"] == str(runtime)
    assert manifest["interpreter"] == str(interpreter)
    assert manifest["files"][".venv/bin/python"] == {
        "symlink": str(runtime / "python/bin/python3.13")
    }
    assert manifest["files"]["python/bin/python3.13"]["mode"] == 0o755
    assert "runtime-manifest.json" not in manifest["files"]


def test_an_interpreter_outside_the_runtime_root_is_refused(root_dir: Path) -> None:
    runtime = root_dir / "runtime"
    (runtime / ".venv" / "bin").mkdir(parents=True)
    outside = root_dir / "python3.13"
    outside.write_text("#!/bin/false\n", encoding="utf-8")
    interpreter = runtime / ".venv" / "bin" / "python"
    interpreter.symlink_to(outside)

    with pytest.raises(runtime_manifest.RuntimeManifestError, match="resolves outside"):
        runtime_manifest.write(runtime, interpreter)


def test_a_runtime_that_moved_no_longer_pins(root_dir: Path) -> None:
    runtime, interpreter = make_runtime(root_dir)
    receipt = runtime_manifest.write(runtime, interpreter)

    (runtime / ".venv" / "lib" / "site-packages" / "__pycache__").mkdir()
    (runtime / ".venv" / "lib" / "site-packages" / "__pycache__" / "dep.pyc").write_bytes(b"\x00")

    with pytest.raises(ValueError, match="inventory changed"):
        runtime_pin.verify(receipt["dependency_runtime"])


def test_the_runtime_manifest_cli_round_trips(root_dir: Path, capsys: Any) -> None:
    runtime, interpreter = make_runtime(root_dir)

    assert (
        runtime_manifest.main(["write", "--root", str(runtime), "--interpreter", str(interpreter)])
        == 0
    )
    written = json.loads(capsys.readouterr().out)
    assert (
        runtime_manifest.main(
            [
                "verify",
                "--root",
                str(runtime),
                "--interpreter",
                str(interpreter),
                "--sha256",
                written["dependency_runtime"]["manifest_sha256"],
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "runtime_manifest_verified"


# ---------------------------------------------------------------------------
# Host binding
# ---------------------------------------------------------------------------


def write_source_config(path: Path, **overrides: Any) -> Path:
    row = {
        "organization_id": ORGANIZATION_ID,
        "principal_id": PRINCIPAL_ID,
        "source_manifest_sha256": "5" * 64,
        "experiment_revision": "1" * 64,
        "issuer_id": "sibyl14-learning-source-deadbeef-oracle",
        "registered_attempts": 240,
        "signed_admissions": 233,
        "volume": "an-extra-key-verify-archive-ignores",
    }
    row.update(overrides)
    path.write_text(json.dumps({"source": row, "unrelated": 1}), encoding="utf-8")
    return path


def write_archive(path: Path) -> str:
    path.write_bytes(b"not a real cohort tar")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_binding_carries_exactly_the_owner_keys(root_dir: Path) -> None:
    stage = stage_a_root(root_dir)
    archive = root_dir / "cohort.tar"
    digest = write_archive(archive)
    config = write_source_config(root_dir / "source-config-v3.json")

    binding = host_binding.build(
        runtime_root=root_dir,
        archive=archive,
        archive_sha256=digest,
        source_config=config,
    )

    assert set(binding) == checkpoints.HOST_BINDING_KEYS
    assert set(binding["owners"]) == checkpoints.OWNER_KEYS
    assert binding["archive"] == {"path": str(archive), "sha256": digest}
    assert binding["owners"]["source"] == str(root_dir / "source")
    assert binding["owners"]["source_commit"] == COMMIT
    assert binding["owners"]["dependency_runtime"] == stage["dependency_runtime"]
    assert binding["owners"]["owned_database_url"] == "ws://127.0.0.1:21642/rpc"


def test_the_binding_digest_is_the_manifest_the_owners_will_read(root_dir: Path) -> None:
    stage_a_root(root_dir)
    archive = root_dir / "cohort.tar"
    digest = write_archive(archive)
    config = write_source_config(root_dir / "source-config-v3.json")

    binding = host_binding.build(
        runtime_root=root_dir,
        archive=archive,
        archive_sha256=digest,
        source_config=config,
    )

    manifest = Path(binding["owners"]["source_manifest"])
    assert manifest == root_dir / "source-manifest.json"
    assert (
        binding["owners"]["source_manifest_sha256"]
        == hashlib.sha256(manifest.read_bytes()).hexdigest()
    )


def test_the_source_row_travels_verbatim(root_dir: Path) -> None:
    stage_a_root(root_dir)
    archive = root_dir / "cohort.tar"
    digest = write_archive(archive)
    config = write_source_config(root_dir / "source-config-v3.json")

    binding = host_binding.build(
        runtime_root=root_dir,
        archive=archive,
        archive_sha256=digest,
        source_config=config,
    )

    assert binding["source"] == json.loads(config.read_bytes())["source"]
    assert binding["source"]["volume"] == "an-extra-key-verify-archive-ignores"


def test_a_source_row_missing_a_read_key_is_refused(root_dir: Path) -> None:
    stage_a_root(root_dir)
    archive = root_dir / "cohort.tar"
    digest = write_archive(archive)
    config = root_dir / "source-config-v3.json"
    row = json.loads(write_source_config(config).read_bytes())
    del row["source"]["signed_admissions"]
    config.write_text(json.dumps(row), encoding="utf-8")

    with pytest.raises(host_binding.HostBindingError, match="signed_admissions"):
        host_binding.build(
            runtime_root=root_dir,
            archive=archive,
            archive_sha256=digest,
            source_config=config,
        )


def test_an_archive_that_does_not_hash_to_its_binding_is_refused(root_dir: Path) -> None:
    stage_a_root(root_dir)
    archive = root_dir / "cohort.tar"
    write_archive(archive)
    config = write_source_config(root_dir / "source-config-v3.json")

    with pytest.raises(host_binding.HostBindingError, match="hashes to"):
        host_binding.build(
            runtime_root=root_dir,
            archive=archive,
            archive_sha256="0" * 64,
            source_config=config,
        )


def test_a_stage_receipt_without_a_runtime_binding_is_refused(root_dir: Path) -> None:
    stage_a_root(root_dir)
    (root_dir / "stage.json").write_text(json.dumps({"source_commit": COMMIT}), encoding="utf-8")
    archive = root_dir / "cohort.tar"
    digest = write_archive(archive)
    config = write_source_config(root_dir / "source-config-v3.json")

    with pytest.raises(host_binding.HostBindingError, match="dependency_runtime"):
        host_binding.build(
            runtime_root=root_dir,
            archive=archive,
            archive_sha256=digest,
            source_config=config,
        )


def test_the_written_binding_loads_through_the_checkpoint_loader(root_dir: Path) -> None:
    stage_a_root(root_dir)
    archive = root_dir / "cohort.tar"
    digest = write_archive(archive)
    config = write_source_config(root_dir / "source-config-v3.json")
    out = root_dir / "host-binding.json"

    assert (
        host_binding.main(
            [
                "--runtime-root",
                str(root_dir),
                "--archive",
                str(archive),
                "--archive-sha256",
                digest,
                "--source-config",
                str(config),
                "--out",
                str(out),
            ]
        )
        == 0
    )

    loaded = checkpoints.host_binding(out)
    assert set(loaded["owners"]) == checkpoints.OWNER_KEYS
    assert os.stat(out).st_mode & 0o777 == 0o600


def test_a_binding_for_another_organization_is_refused(root_dir: Path) -> None:
    stage_a_root(root_dir)
    archive = root_dir / "cohort.tar"
    digest = write_archive(archive)
    config = write_source_config(
        root_dir / "source-config-v3.json", organization_id="00000000-0000-0000-0000-000000000000"
    )
    out = root_dir / "host-binding.json"
    host_binding.write(
        out,
        host_binding.build(
            runtime_root=root_dir,
            archive=archive,
            archive_sha256=digest,
            source_config=config,
        ),
    )

    with pytest.raises(host_binding.HostBindingError, match="another organization"):
        host_binding.validate(out)


def test_the_binding_invents_nothing_beyond_its_four_inputs(root_dir: Path) -> None:
    """Asserting "no sk_ appears" would pass on a fixture that has no key in it.

    The property that actually holds is narrower and checkable: every value in
    the file comes from the archive arguments, the restore config's source row,
    or stage.json. Nothing is read from the environment or from any other file,
    so the only way a credential reaches the binding is if the operator's own
    source row already carries one.
    """
    stage_a_root(root_dir)
    archive = root_dir / "cohort.tar"
    digest = write_archive(archive)
    config = write_source_config(
        root_dir / "source-config-v3.json", stowaway="sk_a_key_the_source_row_carried"
    )
    monkeyed = root_dir / "host-binding.json"
    host_binding.write(
        monkeyed,
        host_binding.build(
            runtime_root=root_dir,
            archive=archive,
            archive_sha256=digest,
            source_config=config,
        ),
    )
    written = json.loads(monkeyed.read_bytes())

    assert written["source"] == json.loads(config.read_bytes())["source"]
    stage = json.loads((root_dir / "stage.json").read_bytes())
    assert written["owners"]["source_commit"] == stage["source_commit"]
    assert written["owners"]["dependency_runtime"] == stage["dependency_runtime"]
    # Outside the verbatim source row, no value carries anything key-shaped.
    elsewhere = json.dumps({k: v for k, v in written.items() if k != "source"})
    assert "sk_" not in elsewhere
    # And the stowaway rode in from the config rather than being invented here.
    assert written["source"]["stowaway"] == "sk_a_key_the_source_row_carried"


def test_the_environment_cannot_reach_the_binding(
    root_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_a_root(root_dir)
    archive = root_dir / "cohort.tar"
    digest = write_archive(archive)
    config = write_source_config(root_dir / "source-config-v3.json")
    monkeypatch.setenv("SCREEN48_OWNER_API_KEY", "sk_must_never_be_read")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk_must_never_be_read_either")

    binding = host_binding.build(
        runtime_root=root_dir,
        archive=archive,
        archive_sha256=digest,
        source_config=config,
    )

    assert "sk_must_never_be_read" not in json.dumps(binding)


# ---------------------------------------------------------------------------
# Preflight phase
# ---------------------------------------------------------------------------


def test_the_registry_knows_preflight_and_keeps_it_off_the_database() -> None:
    assert "preflight" in run_phase.PHASES
    assert set(run_phase.NO_DATABASE_PHASES) == {"preflight"}
    assert set(run_phase.PHASES) - run_phase.NO_DATABASE_PHASES == {
        "cycle",
        "checkpoint0",
        "checkpoint1",
    }


def test_preflight_never_drives_the_owned_container(
    root_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        message = "preflight must not drive the owned container"
        raise AssertionError(message)

    monkeypatch.setattr(owned_db, "start", refuse)
    monkeypatch.setattr(owned_db, "stop", refuse)
    monkeypatch.delenv("SCREEN48_OWNER_API_KEY", raising=False)
    for key in run_phase.PHASE_ENVIRONMENT:
        monkeypatch.setenv(key, "restored-after-this-test")

    exit_code = run_phase.main(
        [
            "preflight",
            "--output",
            str(root_dir / "out"),
            "--host-binding",
            str(root_dir / "absent.json"),
        ]
    )

    record = json.loads((root_dir / "out" / "phase.json").read_text(encoding="utf-8"))
    assert exit_code == run_phase.EXIT_PREFLIGHT_FAILED
    assert record["database_required"] is False
    assert record["database_started"] is False
    assert record["owner_api_key_read"] is False
    assert record["container_id"] is None
    assert record["container_inspect_before"] is None
    assert record["checks"][0]["check"] == "host_binding"
    assert record["checks"][0]["status"] == "failed"
    assert sorted(record["not_covered"]) == ["current_authority", "qualify_originals"]


def test_a_database_phase_still_drives_the_container(
    root_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PHASES signature changed, so prove the other phases kept their database."""
    driven: list[str] = []
    monkeypatch.setattr(owned_db, "start", lambda *a, **k: driven.append("start") or {"Id": "x"})
    monkeypatch.setattr(owned_db, "stop", lambda *a, **k: driven.append("stop") or {"Id": "x"})
    monkeypatch.setattr(run_phase, "_source_commit", lambda: "deadbeef")
    monkeypatch.setattr(owned_db, "wait_ready", lambda *a, **k: {"status": "ok"})
    monkeypatch.setitem(run_phase.PHASES, "cycle", lambda output, extra, record: 0)
    for key in run_phase.PHASE_ENVIRONMENT:
        monkeypatch.setenv(key, "restored-after-this-test")

    exit_code = run_phase.main(["cycle", "--output", str(root_dir / "out")])

    record = json.loads((root_dir / "out" / "phase.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert driven == ["start", "stop"]
    assert record["database_required"] is True
    assert record["container_id"] is not None


def preflight_binding(root_dir: Path, output: Path) -> Path:
    """A staged root plus a binding, ready for a preflight run."""
    stage_a_root(root_dir)
    archive = root_dir / "cohort.tar"
    digest = write_archive(archive)
    config = write_source_config(root_dir / "source-config-v3.json")
    out = root_dir / "host-binding.json"
    host_binding.write(
        out,
        host_binding.build(
            runtime_root=root_dir,
            archive=archive,
            archive_sha256=digest,
            source_config=config,
        ),
    )
    del output
    return out


def run_preflight(root_dir: Path, binding: Path, output: Path) -> dict[str, Any]:
    exit_code = run_phase.main(
        ["preflight", "--output", str(output), "--host-binding", str(binding)]
    )
    record = json.loads((output / "phase.json").read_text(encoding="utf-8"))
    record["_exit_code"] = exit_code
    return record


def test_a_vanished_source_root_lands_as_a_named_check(
    root_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CurrentOwners resolves its source strictly in __init__, before __call__.

    Constructing it outside the per-check capture would send exactly this
    failure, the one preflight exists to name, to the generic handler instead,
    where it never reaches failed_checks.
    """
    monkeypatch.setattr(owned_db, "start", lambda *a, **k: pytest.fail("no container"))
    for key in run_phase.PHASE_ENVIRONMENT:
        monkeypatch.setenv(key, "restored-after-this-test")
    output = root_dir / "out"
    binding = preflight_binding(root_dir, output)
    shutil.rmtree(root_dir / "source")

    record = run_preflight(root_dir, binding, output)

    named = {check["check"]: check for check in record["checks"]}
    assert record["_exit_code"] == run_phase.EXIT_PREFLIGHT_FAILED
    assert record["status"] == "failed"
    assert named["host_binding"]["status"] == "ok"
    # The pin cannot pass under pytest either, because worker=True demands that
    # sys.executable be the binding's own interpreter. What matters is that both
    # land as named checks rather than aborting the phase.
    assert set(named) == {
        "host_binding",
        "dependency_runtime_pin",
        "required_owners_imported",
        "current_owners_qualification",
        "cohort_archive",
        "material_pins",
    }
    qualification = named["current_owners_qualification"]
    assert qualification["status"] == "failed"
    assert "FileNotFoundError" in qualification["error"]
    assert "current_owners_qualification" in record["failed_checks"]


@pytest.mark.parametrize("inside", ["source", "runtime"])
def test_an_output_inside_a_qualified_tree_is_refused(
    root_dir: Path, monkeypatch: pytest.MonkeyPatch, inside: str
) -> None:
    """And refused before anything is written, because the write is the damage.

    A phase record under either manifested root changes the inventory the
    owners qualify, so a refusal that still drops a phase.json there has
    performed the mutation it was meant to prevent.
    """
    monkeypatch.setattr(owned_db, "start", lambda *a, **k: pytest.fail("no container"))
    for key in run_phase.PHASE_ENVIRONMENT:
        monkeypatch.setenv(key, "restored-after-this-test")
    output = root_dir / "out"
    binding = preflight_binding(root_dir, output)
    before = source_manifest.inventory(root_dir / "source")
    forbidden = root_dir / inside / "receipts"

    exit_code = run_phase.main(
        ["preflight", "--output", str(forbidden), "--host-binding", str(binding)]
    )

    assert exit_code == 2
    assert not forbidden.exists()
    assert source_manifest.inventory(root_dir / "source") == before
    runtime_pin.verify(json.loads(binding.read_bytes())["owners"]["dependency_runtime"])


def test_the_output_guard_reads_the_binding_without_importing_the_product(
    root_dir: Path,
) -> None:
    binding = preflight_binding(root_dir, root_dir / "out")

    roots = run_phase.qualified_roots(binding)

    assert [name for name, _ in roots] == ["source", "runtime"]
    assert [path for _, path in roots] == [root_dir / "source", root_dir / "runtime"]
    # An unreadable binding yields no roots rather than an error: the phase
    # itself reports a bad binding far better than the guard can.
    assert run_phase.qualified_roots(root_dir / "absent.json") == []


def test_an_output_outside_both_trees_is_allowed(root_dir: Path) -> None:
    binding = preflight_binding(root_dir, root_dir / "out")

    run_phase.assert_output_outside_qualified_trees(root_dir / "preflight-utc", binding)
    run_phase.assert_output_outside_qualified_trees(root_dir / "sourced-elsewhere", binding)


# ---------------------------------------------------------------------------
# Differential: the mirror against the original
# ---------------------------------------------------------------------------


def current_owners_inventory(source: Path) -> tuple[dict[str, str], dict[str, str]]:
    """The inventory loop transcribed verbatim from CurrentOwners.__call__.

    Kept here on purpose. The manifest writer is only correct insofar as it
    agrees with this loop, and agreement is a thing to measure rather than to
    assert in a docstring.
    """
    files: dict[str, str] = {}
    links: dict[str, str] = {}
    for path in source.rglob("*"):
        rel = str(path.relative_to(source))
        if path.is_symlink():
            links[rel] = str(path.readlink())
            if not path.resolve(strict=True).is_relative_to(source):
                raise AssertionError("source_symlink_escaped")
        elif path.is_file():
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_dir():
            raise AssertionError("unsupported_source_file")
    return files, links


def test_the_manifest_walk_agrees_with_the_qualification_walk(root_dir: Path) -> None:
    source = root_dir / "source"
    (source / "nested" / "deep").mkdir(parents=True)
    (source / "empty").mkdir()
    (source / ".hidden_dir").mkdir()
    (source / ".hidden_file").write_text("hidden\n", encoding="utf-8")
    (source / ".hidden_dir" / "inside.py").write_text("h = 1\n", encoding="utf-8")
    (source / "nested" / "deep" / "leaf.py").write_text("leaf = 1\n", encoding="utf-8")
    (source / "name with spaces.md").write_text("spaced\n", encoding="utf-8")
    (source / "ünïcodé.md").write_text("accented\n", encoding="utf-8")
    executable = source / "run.sh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    (source / "to_file").symlink_to("nested/deep/leaf.py")
    (source / "to_dir").symlink_to("nested")
    (source / "to_link").symlink_to("to_file")

    files, symlinks, _modes = source_manifest.inventory(source)
    expected_files, expected_links = current_owners_inventory(source)

    assert files == expected_files
    assert symlinks == expected_links
    # Neither walk descends through the symlinked directory.
    assert not any(rel.startswith("to_dir/") for rel in files)
    assert set(symlinks) == {"to_file", "to_dir", "to_link"}


def test_eval_issuers_file_wraps_one_issuer_and_names_the_variable(tmp_path: Path) -> None:
    issuer = {
        "issuer_id": "issuer",
        "organization_id": "org",
        "experiment_id": "exp",
        "experiment_revision": "rev",
        "public_key_base64": "AAAA",
        "controller_policy_sha256": "0" * 64,
    }
    path = tmp_path / "issuer.json"
    path.write_text(json.dumps(issuer), encoding="utf-8")
    environ: dict[str, str] = {}

    names = run_phase.apply_environment(environ, eval_issuers_file=path)

    assert json.loads(environ[run_phase.EVAL_ISSUERS_ENV]) == [issuer]
    assert run_phase.EVAL_ISSUERS_ENV in names


def test_eval_issuers_file_refuses_an_incomplete_issuer(tmp_path: Path) -> None:
    path = tmp_path / "issuer.json"
    path.write_text(json.dumps({"issuer_id": "only"}), encoding="utf-8")

    with pytest.raises(run_phase.PhaseError, match="complete issuer"):
        run_phase.load_eval_issuers(path)
