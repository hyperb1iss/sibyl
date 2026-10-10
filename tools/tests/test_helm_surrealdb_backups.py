"""Run the surrealdb chart's rendered backup jobs against a fake HTTP API.

The export and restore-drill CronJobs are shell scripts embedded in the
chart. These tests render them with helm and execute them unchanged, with
real jq and sha256sum, while a fake curl (fixtures/fake_surreal_curl.py)
answers with SurrealDB 3.x response shapes. That proves the script logic
in CI without a database; the shapes themselves were taken from a live
v3.2.4 server.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_CURL = Path(__file__).resolve().parent / "fixtures" / "fake_surreal_curl.py"

_HELM = shutil.which("helm")
pytestmark = pytest.mark.skipif(
    _HELM is None or shutil.which("jq") is None or shutil.which("sha256sum") is None,
    reason="helm, jq and sha256sum are required to run the chart's ops scripts",
)


def kubelet_expand(text: str, env: dict[str, str]) -> str:
    """Expand a container env value or arg the way the kubelet does:
    $$ becomes $, $(NAME) becomes NAME's value when NAME is defined and is
    left alone otherwise, and any other $ is literal."""
    out: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "$" and index + 1 < len(text):
            following = text[index + 1]
            if following == "$":
                out.append("$")
                index += 2
                continue
            if following == "(":
                close = text.find(")", index + 2)
                if close != -1:
                    name = text[index + 2 : close]
                    out.append(env.get(name, f"$({name})"))
                    index = close + 1
                    continue
                out.append("$(")
                index += 2
                continue
            out.append("$" + following)
            index += 2
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


def _render_job(job: str, *overrides: str) -> tuple[str, dict[str, str]]:
    """Return one ops CronJob's shell script and its literal env values,
    both as the container would see them after kubelet expansion."""
    assert _HELM is not None
    rendered = subprocess.run(  # noqa: S603
        [
            _HELM,
            "template",
            "drill",
            "charts/surrealdb",
            "--set",
            "export.enabled=true",
            "--set",
            "restoreDrill.enabled=true",
            *overrides,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    for document in yaml.safe_load_all(rendered.stdout):
        if not document or document.get("kind") != "CronJob":
            continue
        if not document["metadata"]["name"].endswith(f"-{job}"):
            continue
        container = document["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        env: dict[str, str] = {}
        for item in container["env"]:
            if "value" in item:
                env[item["name"]] = kubelet_expand(item["value"], env)
        return kubelet_expand(container["args"][0], env), env
    pytest.fail(f"{job} CronJob not rendered")


class OpsHarness:
    """A scratch root with a backups directory and a fake SurrealDB API."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.backups = root / "backups"
        self.backups.mkdir()
        (root / "tmp").mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        shim = self.bin / "curl"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CURL}" "$@"\n')
        shim.chmod(0o755)
        self.state_path = root / "surreal-state.json"
        self.receipt_path = root / "receipt.json"
        self._values_counter = itertools.count()

    def serve(
        self,
        *,
        source: dict[str, dict[str, dict[str, int]]] | None = None,
        restore: dict[str, dict[str, dict[str, int]]] | None = None,
        empty_exports: tuple[str, ...] = (),
        drop_on_import: tuple[str, ...] = (),
        sql_errors: tuple[str, ...] = (),
        refuse_import: tuple[str, ...] = (),
        drop_tables_on_import: tuple[str, ...] = (),
        shrink_on_import: dict[str, int] | None = None,
        ns_info_errors: tuple[str, ...] = (),
    ) -> None:
        """Describe the fake servers and the faults they inject. Each fault
        names ``ns/db`` pairs (``ns/db/table`` for the table faults): an
        empty export, an import that zeroes every row, a refused row count,
        a refused import, a table that vanishes on import, or one that comes
        back with the given number of rows. ``ns_info_errors`` refuses INFO
        FOR NS for whole namespaces."""
        servers: dict[str, object] = {
            "http://restore": {"user": "drill:drill", "namespaces": restore or {}},
        }
        if source is not None:
            servers["http://source"] = {
                "user": "root:secret",
                "version": "surrealdb-3.2.4",
                "namespaces": source,
            }
        self.state_path.write_text(
            json.dumps(
                {
                    "servers": servers,
                    "empty_exports": list(empty_exports),
                    "drop_on_import": list(drop_on_import),
                    "sql_errors": list(sql_errors),
                    "refuse_import": list(refuse_import),
                    "drop_tables_on_import": list(drop_tables_on_import),
                    "shrink_on_import": shrink_on_import or {},
                    "ns_info_errors": list(ns_info_errors),
                }
            )
        )

    def values(self, values: dict[str, Any]) -> tuple[str, str]:
        """Write chart values to a file and return the helm arguments for it.
        Multi-line shell hooks survive a values file intact, where --set
        would split them on commas and braces."""
        path = self.root / f"values-{next(self._values_counter)}.yaml"
        path.write_text(yaml.safe_dump(values))
        return "-f", str(path)

    def server(self, endpoint: str) -> dict[str, Any]:
        return json.loads(self.state_path.read_text())["servers"][endpoint]

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text())

    def run(self, job: str, *overrides: str) -> subprocess.CompletedProcess[str]:
        script, rendered_env = _render_job(job, *overrides)
        env = {
            **rendered_env,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.root),
            "TMPDIR": str(self.root / "tmp"),
            "FAKE_SURREAL_STATE": str(self.state_path),
            "SURREAL_ENDPOINT": "http://source",
            "SURREAL_PASS": "secret",
            "SURREAL_RESTORE_ENDPOINT": "http://restore",
            "SIBYL_EXPORT_DESTINATION_PATH": str(self.backups),
            "SIBYL_RESTORE_SOURCE_PATH": str(self.backups),
            "SIBYL_RESTORE_RECEIPT_PATH": str(self.receipt_path),
            "POD_NAMESPACE": "sibyl",
        }
        return subprocess.run(  # noqa: S603
            ["/bin/sh", "-ec", script],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )


@pytest.fixture
def harness(tmp_path: Path) -> OpsHarness:
    return OpsHarness(tmp_path)


def test_helm_restore_drill_notifies_on_every_failure(harness: OpsHarness) -> None:
    """A drill that finds nothing to restore fails through an explicit
    exit, which busybox ash never reports to an ERR trap."""
    harness.serve()
    result = harness.run(
        "restore-drill",
        "--set-string",
        'restoreDrill.failureNotification.command=echo notified >> "$HOME/notified.log"',
    )

    assert result.returncode != 0
    assert (harness.root / "notified.log").read_text() == "notified\n"
    assert not harness.receipt_path.exists()


ORG_A = "org_0b8e5a3c1f2d4e6a9b7c8d9e0f1a2b3c"
ORG_B = "org_5d4c3b2a1f0e4d9c8b7a6f5e4d3c2b1a"


def _source() -> dict[str, dict[str, dict[str, int]]]:
    """A server shaped like a live Sibyl: auth, content, two org graphs,
    and SurrealDB's own empty default namespace."""
    return {
        "main": {"main": {}},
        "sibyl_auth": {"auth": {"users": 2, "organizations": 2, "api_keys": 0}},
        "sibyl_content": {"content": {"documents": 3, "raw_captures": 0}},
        ORG_A: {"graph": {"entity": 7, "relates_to": 6, "schema_version": 1}},
        ORG_B: {"graph": {"entity": 4, "relates_to": 3, "schema_version": 1}},
    }


SOURCE_DATABASES = sum(len(databases) for databases in _source().values())
MANIFEST_VERSION = 2
RECORDED_FAILURE_EXIT = 111
DEFAULT_DEADLINE_SECONDS = 21600
CUSTOM_BACKOFF = 2
CUSTOM_DEADLINE_SECONDS = 600
DEFAULT_MAX_AGE_HOURS = 36
FRESH_SECONDS = 600


def _run_dirs(harness: OpsHarness) -> list[Path]:
    return sorted(path for path in harness.backups.iterdir() if path.is_dir())


def _manifest(harness: OpsHarness) -> dict[str, Any]:
    (run_dir,) = _run_dirs(harness)
    return json.loads((run_dir / "manifest.json").read_text())


def test_helm_export_discovers_every_namespace_without_a_list(harness: OpsHarness) -> None:
    """Org graphs live in org_<uuid> namespaces created at run time; the
    static `databases` list (bootstrap only) must not limit the export."""
    harness.serve(source=_source())
    result = harness.run(
        "export",
        "--set",
        "databases[0].namespace=sibyl_auth",
        "--set",
        "databases[0].database=auth",
    )
    assert result.returncode == 0, result.stderr

    manifest = _manifest(harness)
    (run_dir,) = _run_dirs(harness)
    assert run_dir.name.startswith("sibyl-")
    assert manifest["format"] == "sibyl-surrealdb-export"
    assert manifest["version"] == MANIFEST_VERSION
    assert manifest["failures"] == []
    assert manifest["server_version"] == "surrealdb-3.2.4"
    assert manifest["created_at"].endswith("Z")
    exported = {(entry["namespace"], entry["database"]): entry for entry in manifest["databases"]}
    assert set(exported) == {
        ("main", "main"),
        ("sibyl_auth", "auth"),
        ("sibyl_content", "content"),
        (ORG_A, "graph"),
        (ORG_B, "graph"),
    }
    for (namespace, database), entry in exported.items():
        data = (run_dir / entry["file"]).read_bytes()
        assert entry["file"] == f"{namespace}.{database}.surql"
        assert entry["bytes"] == len(data) > 0
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["tables"] == _source()[namespace][database]
        assert entry["rows"] == sum(_source()[namespace][database].values())


def test_helm_bootstrap_list_is_required_but_never_iterated() -> None:
    """The scripts do not loop over `databases`; the list reaches them
    only as the set of databases every backup must contain."""
    default_export, _ = _render_job("export")
    default_drill, _ = _render_job("restore-drill")
    custom = ("--set", "databases[0].namespace=only_this", "--set", "databases[0].database=db")

    export_script, export_env = _render_job("export", *custom)
    drill_script, drill_env = _render_job("restore-drill", *custom)
    assert export_script == default_export
    assert drill_script == default_drill
    required = [{"namespace": "only_this", "database": "db"}]
    assert json.loads(export_env["SIBYL_EXPORT_REQUIRED_DATABASES"]) == required
    assert json.loads(drill_env["SIBYL_RESTORE_REQUIRED_DATABASES"]) == required


# A reversible stand-in for a real cipher: a header line plus rot13. The
# output is not JSON, so a drill that skips decryption cannot read it.
CIPHER_HEADER = "SIBYL-TEST-CIPHER"
ENCRYPT = (
    f'( echo {CIPHER_HEADER}; tr "A-Za-z" "N-ZA-Mn-za-m" < "$SIBYL_EXPORT_FILE" )'
    ' > "$SIBYL_EXPORT_ENCRYPTED_FILE"'
)
DECRYPT = (
    'tail -n +2 "$SIBYL_RESTORE_ENCRYPTED_FILE" | tr "A-Za-z" "N-ZA-Mn-za-m"'
    ' > "$SIBYL_RESTORE_FILE"'
)
NOTIFY = 'echo notified >> "$HOME/notified.log"'


def _encrypted_export_values() -> dict[str, Any]:
    return {"export": {"encryption": {"enabled": True, "command": ENCRYPT}}}


def _decrypting_drill_values(command: str = DECRYPT) -> dict[str, Any]:
    return {
        "restoreDrill": {
            "decryption": {"enabled": True, "command": command},
            "failureNotification": {"command": NOTIFY},
        }
    }


def _stored_files(run_dir: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(run_dir.iterdir())}


def test_helm_export_hooks_cover_every_file_and_the_manifest(harness: OpsHarness) -> None:
    harness.serve(source=_source())
    result = harness.run(
        "export",
        *harness.values(
            {
                "export": {
                    "encryption": {
                        "enabled": True,
                        "command": f'echo "$SIBYL_EXPORT_FILE" >> "$HOME/encrypted.log"\n{ENCRYPT}',
                    },
                    "syncCommand": 'echo "$SIBYL_EXPORT_RUN_DIR" >> "$HOME/synced.log"',
                }
            }
        ),
    )
    assert result.returncode == 0, result.stderr

    (run_dir,) = _run_dirs(harness)
    stored = _stored_files(run_dir)
    files = sorted(name for name in stored if name.endswith(".surql"))
    assert set(stored) == {*files, "manifest.json"}
    assert len(files) == SOURCE_DATABASES
    encrypted = (harness.root / "encrypted.log").read_text().splitlines()
    assert sorted(Path(line).name for line in encrypted[:-1]) == files
    assert Path(encrypted[-1]).name == "manifest.json"
    for line in encrypted:
        assert not line.startswith(str(harness.backups)), "plaintext must stay in scratch"
    assert list((harness.root / "tmp").iterdir()) == [], (
        "scratch plaintext must not outlive the job"
    )
    for name, data in stored.items():
        assert data.startswith(CIPHER_HEADER.encode()), name
    assert (harness.root / "synced.log").read_text().splitlines() == [str(run_dir)]


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("true", "wrote nothing to $SIBYL_EXPORT_ENCRYPTED_FILE"),
        (': > "$SIBYL_EXPORT_ENCRYPTED_FILE"', "wrote nothing to $SIBYL_EXPORT_ENCRYPTED_FILE"),
        ('mv "$SIBYL_EXPORT_FILE" "$SIBYL_EXPORT_FILE.enc"', "wrote nothing"),
        ('cp "$SIBYL_EXPORT_FILE" "$SIBYL_EXPORT_ENCRYPTED_FILE"', "without encrypting it"),
        ("echo cipher-tool-missing >&2; false", "cipher-tool-missing"),
    ],
    ids=["writes-nothing", "writes-empty-file", "renames", "copies-plaintext", "fails"],
)
def test_helm_export_refuses_encryption_that_does_not_encrypt(
    harness: OpsHarness, command: str, message: str
) -> None:
    harness.serve(source=_source())
    result = harness.run(
        "export", *harness.values({"export": {"encryption": {"enabled": True, "command": command}}})
    )

    assert result.returncode != 0
    assert message in result.stderr
    assert list(harness.backups.iterdir()) == [], "a failed run removes its own directory"
    assert list((harness.root / "tmp").iterdir()) == [], "and its scratch plaintext"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ({}, "discovery found no databases"),
        ({"ns_only": {}}, "discovery found no databases"),
        ({"main": {"main": {}}}, "besides SurrealDB's default main/main"),
        (
            {"main": {"main": {}}, "other_app": {"data": {"t": 5}}},
            "required databases missing at http://source: sibyl_auth/auth, sibyl_content/content",
        ),
        (
            {key: value for key, value in _source().items() if key != "sibyl_content"},
            "required databases missing at http://source: sibyl_content/content",
        ),
    ],
    ids=["no-namespaces", "no-databases", "only-default-main", "wrong-server", "missing-content"],
)
def test_helm_export_refuses_an_empty_or_wrong_server(
    harness: OpsHarness, source: dict[str, dict[str, dict[str, int]]], message: str
) -> None:
    """SurrealDB always has main/main, so finding it proves nothing, and a
    server without the bootstrap databases is not the Sibyl server."""
    harness.serve(source=source)
    result = harness.run("export")

    assert result.returncode != 0
    assert message in result.stderr
    assert list(harness.backups.iterdir()) == []


Configure = Callable[[OpsHarness], None]


def _export_then_reset(harness: OpsHarness, configure: Configure | None = None) -> Path:
    """Export the standard source, then give the drill an empty restore
    server, optionally with faults."""
    harness.serve(source=_source())
    exported = harness.run("export")
    assert exported.returncode == 0, exported.stderr
    if configure is None:
        harness.serve()
    else:
        configure(harness)
    (run_dir,) = _run_dirs(harness)
    return run_dir


def test_helm_restore_drill_restores_every_database_in_the_newest_manifest(
    harness: OpsHarness,
) -> None:
    run_dir = _export_then_reset(harness)
    stale = harness.backups / "sibyl-20000101000000"
    stale.mkdir()
    (stale / "manifest.json").write_text("not json: an older run the drill must not read")
    (harness.backups / "sibyl-99991231235959").mkdir()  # newer, but never completed
    staging = harness.backups / "sibyl-staging-99991231235959"  # another prefix's run
    staging.mkdir()
    (staging / "manifest.json").write_text("not json: a sibling prefix the drill must not read")

    result = harness.run("restore-drill")
    assert result.returncode == 0, result.stderr
    assert "skipping incomplete run sibyl-99991231235959 (no manifest.json)" in result.stdout
    assert "sibyl-staging" not in result.stdout + result.stderr

    receipt = json.loads(harness.receipt_path.read_text())
    assert receipt["status"] == "PASS"
    assert receipt["manifest"]["path"] == str(run_dir / "manifest.json")
    assert receipt["manifest"]["databases"] == SOURCE_DATABASES
    assert 0 <= receipt["manifest"]["age_seconds"] < FRESH_SECONDS
    assert receipt["manifest"]["max_age_hours"] == DEFAULT_MAX_AGE_HOURS
    assert receipt["manifest"]["decrypted"] is False
    by_name = {(item["namespace"], item["database"]): item for item in receipt["databases"]}
    assert set(by_name) == {(ns, db) for ns, dbs in _source().items() for db in dbs}
    for (namespace, database), item in by_name.items():
        expected = _source()[namespace][database]
        assert item["status"] == "PASS"
        assert item["rows_exported"] == item["rows_restored"] == sum(expected.values())
        assert item["tables"] == {
            table: {"exported": rows, "restored": rows} for table, rows in expected.items()
        }
    assert receipt["row_counts"] == {"sibyl_auth.auth.users": {"expected": 2, "actual": 2}}
    restore = harness.server("http://restore")
    assert restore["namespaces"] == _source()
    assert restore["strict"] == ["sibyl_auth/auth", "sibyl_content/content"]


def _flip_letters(path: Path) -> None:
    """Change content without changing size, so only sha256 can tell."""
    text = path.read_text()
    assert "a" in text
    path.write_text(text.replace("a", "\0").replace("b", "a").replace("\0", "b"))


@pytest.mark.parametrize(
    ("damage", "configure", "message"),
    [
        ("tamper", None, "does not match the manifest's size and sha256"),
        ("same-size", None, "does not match the manifest's size and sha256"),
        ("delete", None, "is missing"),
        (
            None,
            lambda h: h.serve(drop_on_import=(f"{ORG_A}/graph",)),
            "tables short after import: entity (0 of 7, emptied), relates_to (0 of 6, emptied)",
        ),
        (
            None,
            lambda h: h.serve(shrink_on_import={f"{ORG_A}/graph/entity": 0}),
            "tables short after import: entity (0 of 7, emptied)",
        ),
        (
            None,
            lambda h: h.serve(drop_tables_on_import=(f"{ORG_A}/graph/relates_to",)),
            "tables missing after import: relates_to",
        ),
        (
            None,
            lambda h: h.serve(refuse_import=(f"{ORG_A}/graph",)),
            "import failed: ",
        ),
    ],
    ids=[
        "checksum",
        "same-size-tamper",
        "missing-file",
        "empty-restore",
        "one-empty-table",
        "missing-table",
        "refused-import",
    ],
)
def test_helm_restore_drill_fails_and_notifies_on_a_bad_database(
    harness: OpsHarness,
    damage: str | None,
    configure: Configure | None,
    message: str,
) -> None:
    run_dir = _export_then_reset(harness, configure)
    target = run_dir / f"{ORG_A}.graph.surql"
    if damage == "tamper":
        target.write_text(target.read_text() + "\n-- tampered\n")
    elif damage == "same-size":
        _flip_letters(target)
    elif damage == "delete":
        target.unlink()

    result = harness.run(
        "restore-drill",
        "--set-string",
        'restoreDrill.failureNotification.command=echo notified >> "$HOME/notified.log"',
    )

    assert result.returncode != 0
    assert f"restore failed for {ORG_A}/graph: " in result.stderr
    assert message in result.stderr
    assert f"restore failed for {ORG_B}/graph" not in result.stderr
    assert (harness.root / "notified.log").read_text() == "notified\n"
    assert not harness.receipt_path.exists()


def test_helm_restore_drill_scratch_server_keeps_data_on_its_tmp_volume() -> None:
    """The drill now restores every org's graph, so the scratch sidecar's
    default store must be disk on its own /tmp emptyDir, not RAM."""
    assert _HELM is not None
    rendered = subprocess.run(  # noqa: S603
        [_HELM, "template", "drill", "charts/surrealdb", "--set", "restoreDrill.enabled=true"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    for document in yaml.safe_load_all(rendered.stdout):
        if document and document.get("kind") == "CronJob":
            pod = document["spec"]["jobTemplate"]["spec"]["template"]["spec"]
            (server,) = (c for c in pod["initContainers"] if c["name"] == "restore-server")
            env = {item["name"]: item.get("value") for item in server["env"]}
            assert env["SURREAL_RESTORE_PATH"] == "rocksdb:/tmp/restore-db"
            store_path = env["SURREAL_RESTORE_PATH"].removeprefix("rocksdb:")
            tmp_mount = next(
                m for m in server["volumeMounts"] if store_path.startswith(m["mountPath"] + "/")
            )
            tmp_volume = next(v for v in pod["volumes"] if v["name"] == tmp_mount["name"])
            assert "emptyDir" in tmp_volume
            return
    pytest.fail("restore-drill CronJob not rendered")


def test_helm_restore_drill_decrypts_into_scratch_and_checks_the_plaintext(
    harness: OpsHarness,
) -> None:
    harness.serve(source=_source())
    exported = harness.run("export", *harness.values(_encrypted_export_values()))
    assert exported.returncode == 0, exported.stderr
    harness.serve()
    (run_dir,) = _run_dirs(harness)
    before = _stored_files(run_dir)

    result = harness.run("restore-drill", *harness.values(_decrypting_drill_values()))
    assert result.returncode == 0, result.stderr

    receipt = json.loads(harness.receipt_path.read_text())
    assert receipt["status"] == "PASS"
    assert receipt["manifest"]["decrypted"] is True
    assert {item["status"] for item in receipt["databases"]} == {"PASS"}
    assert harness.server("http://restore")["namespaces"] == _source()
    assert _stored_files(run_dir) == before, "the drill must never rewrite the backup it reads"
    assert harness.state()["max_import_siblings"] == 1, "one decrypted file at a time"
    assert list((harness.root / "tmp").iterdir()) == [], (
        "decrypted copies must not outlive the drill"
    )


@pytest.mark.parametrize(
    ("drill_values", "message"),
    [
        ({}, "is not JSON; encrypted exports need restoreDrill.decryption"),
        (
            _decrypting_drill_values('cp "$SIBYL_RESTORE_ENCRYPTED_FILE" "$SIBYL_RESTORE_FILE"'),
            "is not JSON",
        ),
        (
            _decrypting_drill_values(
                DECRYPT
                + '\ncase "$SIBYL_RESTORE_FILE" in *.surql) echo tampered >> "$SIBYL_RESTORE_FILE";; esac'
            ),
            f"restore failed for {ORG_A}/graph: export file",
        ),
        (
            _decrypting_drill_values(
                DECRYPT
                + '\ncase "$SIBYL_RESTORE_FILE" in *.surql) tr ab ba < "$SIBYL_RESTORE_FILE"'
                ' > "$SIBYL_RESTORE_FILE.x"; mv "$SIBYL_RESTORE_FILE.x" "$SIBYL_RESTORE_FILE";; esac'
            ),
            f"restore failed for {ORG_A}/graph: export file",
        ),
        (
            _decrypting_drill_values(
                'case "$SIBYL_RESTORE_FILE" in *.surql) exit 4;; esac\n' + DECRYPT
            ),
            f"restore failed for {ORG_A}/graph: decryption failed",
        ),
    ],
    ids=["no-decryption", "wrong-key", "wrong-plaintext", "same-size-plaintext", "decrypt-error"],
)
def test_helm_restore_drill_fails_when_decryption_is_missing_or_wrong(
    harness: OpsHarness, drill_values: dict[str, Any], message: str
) -> None:
    harness.serve(source=_source())
    exported = harness.run("export", *harness.values(_encrypted_export_values()))
    assert exported.returncode == 0, exported.stderr
    harness.serve()

    values = {"restoreDrill": {"failureNotification": {"command": NOTIFY}}}
    values["restoreDrill"].update(drill_values.get("restoreDrill", {}))
    result = harness.run("restore-drill", *harness.values(values))

    assert result.returncode != 0
    assert message in result.stderr
    assert (harness.root / "notified.log").read_text() == "notified\n"
    assert not harness.receipt_path.exists()


# Mirrors the documented S3 fetch with a directory standing in for the
# bucket: newest run whose manifest exists, files first, manifest last.
# The `exit 0` also proves the hook cannot end the drill early.
FETCH = """\
remote="$SIBYL_EXPORT_DESTINATION_URI"
for run in $(ls -1 "$remote" | grep -E "^$SIBYL_EXPORT_FILE_PREFIX-[0-9]{14}$" | sort -r); do
  if [ -f "$remote/$run/manifest.json" ]; then
    mkdir -p "$SIBYL_RESTORE_SOURCE_PATH/$run"
    for file in "$remote/$run"/*.surql; do cp "$file" "$SIBYL_RESTORE_SOURCE_PATH/$run/"; done
    cp "$remote/$run/manifest.json" "$SIBYL_RESTORE_SOURCE_PATH/$run/manifest.json"
    exit 0
  fi
done
echo "no complete run under $remote" >&2
exit 1
"""


def test_helm_restore_drill_fetches_the_newest_complete_run(harness: OpsHarness) -> None:
    run_dir = _export_then_reset(harness)
    remote = harness.root / "bucket"
    remote.mkdir()
    shutil.move(run_dir, remote / run_dir.name)
    unfinished = remote / "sibyl-99991231235959"
    unfinished.mkdir()
    (unfinished / f"{ORG_A}.graph.surql").write_text("-- upload still running\n")
    staging = remote / "sibyl-staging-99991231235959"
    shutil.copytree(remote / run_dir.name, staging)
    (staging / "manifest.json").write_text("not json: a sibling prefix the fetch must not take")

    result = harness.run(
        "restore-drill",
        *harness.values(
            {
                "export": {"destination": {"uri": str(remote)}},
                "restoreDrill": {"fetchCommand": FETCH},
            }
        ),
    )
    assert result.returncode == 0, result.stderr

    receipt = json.loads(harness.receipt_path.read_text())
    assert receipt["status"] == "PASS"
    assert receipt["manifest"]["path"] == str(harness.backups / run_dir.name / "manifest.json")
    assert [path.name for path in _run_dirs(harness)] == [run_dir.name]
    assert harness.server("http://restore")["namespaces"] == _source()


def _age_manifest(run_dir: Path, hours: float) -> None:
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    created = datetime.now(UTC) - timedelta(hours=hours)
    manifest["created_at"] = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_path.write_text(json.dumps(manifest))


@pytest.mark.parametrize(
    ("max_age_hours", "passes"),
    [(None, False), (48, True), (0, True)],
    ids=["default-36h", "raised-to-48h", "disabled"],
)
def test_helm_restore_drill_pages_when_the_newest_export_is_stale(
    harness: OpsHarness, max_age_hours: int | None, *, passes: bool
) -> None:
    run_dir = _export_then_reset(harness)
    _age_manifest(run_dir, hours=40)
    drill: dict[str, Any] = {"failureNotification": {"command": NOTIFY}}
    if max_age_hours is not None:
        drill["maxExportAgeHours"] = max_age_hours

    result = harness.run("restore-drill", *harness.values({"restoreDrill": drill}))

    if passes:
        assert result.returncode == 0, result.stderr
        manifest = json.loads(harness.receipt_path.read_text())["manifest"]
        assert abs(manifest["age_seconds"] - 40 * 3600) < FRESH_SECONDS
        assert manifest["max_age_hours"] == max_age_hours
    else:
        assert result.returncode != 0
        assert "40h ago; restoreDrill.maxExportAgeHours is 36" in result.stderr
        assert (harness.root / "notified.log").read_text() == "notified\n"
        assert not harness.receipt_path.exists()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (
            ("--set", "restoreDrill.decryption.enabled=true"),
            "restoreDrill.decryption.command is required",
        ),
        (("--set-string", "restoreDrill.maxExportAgeHours=abc"), "whole number of hours"),
        (("--set", "restoreDrill.maxExportAgeHours=-1"), "whole number of hours"),
        (("--set", "restoreDrill.maxExportAgeHours=1.5"), "whole number of hours"),
    ],
    ids=["decryption-without-command", "age-text", "age-negative", "age-fraction"],
)
def test_helm_restore_drill_rejects_unusable_hook_values(
    override: tuple[str, str], message: str
) -> None:
    assert _HELM is not None
    rendered = subprocess.run(  # noqa: S603
        [
            _HELM,
            "template",
            "drill",
            "charts/surrealdb",
            "--set",
            "restoreDrill.enabled=true",
            *override,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode != 0
    assert message in rendered.stderr


# Names SurrealDB accepts but a plain identifier would not: they must be
# exported (headers carry printable ASCII) and restored with escaping.
ODD_NAMES: dict[str, dict[str, dict[str, int]]] = {
    "legacy-tenant": {"graph": {"entity": 3}},
    "odd ns": {"odd db": {"thing": 2}},
    "back`tick\\slash": {"graph": {"entity": 1}},
}


def _odd_file(namespace: str, database: str) -> str:
    def safe(name: str) -> str:
        return "".join(ch if ch.isascii() and (ch.isalnum() or ch == "_") else "_" for ch in name)

    digest = hashlib.sha256(f"{namespace}\n{database}".encode()).hexdigest()[:12]
    return f"{safe(namespace)}.{safe(database)}.{digest}.surql"


def test_helm_odd_names_export_and_restore_with_escaping(harness: OpsHarness) -> None:
    harness.serve(source={**_source(), **ODD_NAMES})
    exported = harness.run("export")
    assert exported.returncode == 0, exported.stderr

    manifest = _manifest(harness)
    files = {
        (entry["namespace"], entry["database"]): entry["file"] for entry in manifest["databases"]
    }
    for namespace, databases in ODD_NAMES.items():
        for database in databases:
            assert files[(namespace, database)] == _odd_file(namespace, database)
    assert manifest["failures"] == []

    harness.serve()
    drilled = harness.run("restore-drill")
    assert drilled.returncode == 0, drilled.stderr
    assert harness.server("http://restore")["namespaces"] == {**_source(), **ODD_NAMES}


def test_helm_export_records_what_it_cannot_export_and_keeps_the_rest(
    harness: OpsHarness,
) -> None:
    """One unusable name or one failing database must not cost every other
    backup: the run exports the rest, records each failure in the manifest,
    syncs, and then fails so it pages."""
    unsafe = {"tenant-ü": {"graph": {"entity": 1}}, "bad\nline": {"graph": {"entity": 1}}}
    harness.serve(
        source={**_source(), **ODD_NAMES, **unsafe, "ok_ns": {"weird\tdb": {"t": 1}}},
        empty_exports=(f"{ORG_B}/graph",),
        sql_errors=("legacy-tenant/graph",),
    )
    result = harness.run(
        "export",
        *harness.values({"export": {"syncCommand": 'echo synced >> "$HOME/synced.log"'}}),
    )

    assert result.returncode == RECORDED_FAILURE_EXIT
    assert "failures recorded in" in result.stderr
    assert (harness.root / "synced.log").read_text() == "synced\n", "the partial run still syncs"
    manifest = _manifest(harness)
    exported = {(entry["namespace"], entry["database"]) for entry in manifest["databases"]}
    assert ("sibyl_auth", "auth") in exported
    assert (ORG_A, "graph") in exported
    assert ("odd ns", "odd db") in exported
    reasons = {(f["namespace"], f["database"]): f["reason"] for f in manifest["failures"]}
    assert set(reasons) == {
        ("tenant-ü", None),
        ("bad\nline", None),
        ("ok_ns", "weird\tdb"),
        (ORG_B, "graph"),
        ("legacy-tenant", "graph"),
    }
    assert "surreal-ns header" in reasons[("tenant-ü", None)]
    assert "surreal-db header" in reasons[("ok_ns", "weird\tdb")]
    assert reasons[(ORG_B, "graph")] == "the export came back empty"
    assert "SurrealQL failed in legacy-tenant/graph" in reasons[("legacy-tenant", "graph")]

    harness.serve()
    drilled = harness.run(
        "restore-drill",
        *harness.values({"restoreDrill": {"failureNotification": {"command": NOTIFY}}}),
    )
    assert drilled.returncode != 0
    assert 'export recorded a failure for "tenant-ü"/"*"' in drilled.stderr
    assert (harness.root / "notified.log").read_text() == "notified\n"
    restored = harness.server("http://restore")["namespaces"]
    assert restored[ORG_A] == _source()[ORG_A], "the rest of the backup still restores"


def test_helm_restore_drill_requires_the_bootstrap_databases(harness: OpsHarness) -> None:
    run_dir = _export_then_reset(harness)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["databases"] = [e for e in manifest["databases"] if e["namespace"] != "sibyl_content"]
    manifest_path.write_text(json.dumps(manifest))

    result = harness.run("restore-drill")

    assert result.returncode != 0
    assert "is missing required databases: sibyl_content/content" in result.stderr


@pytest.mark.parametrize(
    ("entity_rows", "passes"),
    [(5, True), (4, False)],
    ids=["within-drift", "beyond-drift"],
)
def test_helm_restore_drill_allows_drift_per_table(
    harness: OpsHarness, entity_rows: int, *, passes: bool
) -> None:
    """entity exports 7 rows; the default allowance is max(2 rows, 1%)."""
    _export_then_reset(
        harness, lambda h: h.serve(shrink_on_import={f"{ORG_A}/graph/entity": entity_rows})
    )

    result = harness.run("restore-drill")

    assert (result.returncode == 0) is passes, result.stderr
    if not passes:
        assert f"entity ({entity_rows} of 7, allowed shortfall 2)" in result.stderr


def test_helm_export_prunes_only_its_own_stale_incomplete_runs(harness: OpsHarness) -> None:
    recent = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y%m%d%H%M%S")
    layout = {
        "sibyl-20000101000000": False,  # incomplete, old: pruned
        f"sibyl-{recent}": False,  # incomplete, recent: may still be running
        "sibyl-20000101000001": True,  # complete, old: retention's business
        "sibyl-staging-20000101000000": False,  # another prefix
    }
    for name, complete in layout.items():
        (harness.backups / name).mkdir()
        (harness.backups / name / "partial.surql").write_text("-- partial\n")
        if complete:
            (harness.backups / name / "manifest.json").write_text("{}")
    harness.serve(source=_source())

    result = harness.run("export")

    assert result.returncode == 0, result.stderr
    assert "pruned incomplete run sibyl-20000101000000" in result.stdout
    remaining = {path.name for path in harness.backups.iterdir()}
    assert "sibyl-20000101000000" not in remaining
    assert {f"sibyl-{recent}", "sibyl-20000101000001", "sibyl-staging-20000101000000"} <= remaining


@pytest.mark.parametrize(
    ("text", "env", "expected"),
    [
        ("$$", {}, "$"),
        ("$$$$", {}, "$$"),
        ("$(HOME)", {"HOME": "/home/ops"}, "/home/ops"),
        ("$(UNSET)", {}, "$(UNSET)"),
        ("$(date -u +%s)", {}, "$(date -u +%s)"),
        ("$((1 + 2))", {}, "$((1 + 2))"),
        ("${x:-y} $x", {}, "${x:-y} $x"),
        ("tail$", {}, "tail$"),
    ],
)
def test_helm_harness_expands_like_the_kubelet(
    text: str, env: dict[str, str], expected: str
) -> None:
    assert kubelet_expand(text, env) == expected


def test_helm_hooks_survive_kubelet_expansion(harness: OpsHarness) -> None:
    """The kubelet collapses $$ and expands $(NAME) in env values and args.
    Hooks are escaped at render time so they reach the shell as written."""
    hook = '[ "$$" -gt 0 ]; echo "pid ok $(echo HOME)" >> "$HOME/hooks.log"'
    _, raw_env = _raw_env(
        "export",
        "--set",
        "export.encryption.enabled=true",
        "--set-string",
        f"export.encryption.command={hook}",
    )
    assert raw_env["SIBYL_EXPORT_ENCRYPTION_COMMAND"] == hook.replace("$", "$$")

    harness.serve(source=_source())
    result = harness.run(
        "export",
        *harness.values(
            {
                "export": {
                    "encryption": {"enabled": True, "command": f"{hook}\n{ENCRYPT}"},
                    "syncCommand": hook,
                }
            }
        ),
    )
    assert result.returncode == 0, result.stderr
    lines = (harness.root / "hooks.log").read_text().splitlines()
    assert lines == ["pid ok HOME"] * (SOURCE_DATABASES + 2)


def _raw_env(job: str, *overrides: str) -> tuple[dict[str, Any], dict[str, str]]:
    """The rendered job spec and env exactly as written to the manifest,
    before the kubelet touches them."""
    assert _HELM is not None
    rendered = subprocess.run(  # noqa: S603
        [
            _HELM,
            "template",
            "drill",
            "charts/surrealdb",
            "--set",
            "export.enabled=true",
            "--set",
            "restoreDrill.enabled=true",
            *overrides,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    for document in yaml.safe_load_all(rendered.stdout):
        if (
            document
            and document.get("kind") == "CronJob"
            and document["metadata"]["name"].endswith(f"-{job}")
        ):
            container = document["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
            env = {item["name"]: item["value"] for item in container["env"] if "value" in item}
            return document["spec"]["jobTemplate"]["spec"], env
    pytest.fail(f"{job} CronJob not rendered")


def test_helm_jobs_are_bounded_and_the_drill_pages_once() -> None:
    export_spec, export_env = _raw_env("export")
    drill_spec, drill_env = _raw_env("restore-drill")

    assert drill_spec["backoffLimit"] == 0
    assert (
        export_spec["activeDeadlineSeconds"]
        == drill_spec["activeDeadlineSeconds"]
        == DEFAULT_DEADLINE_SECONDS
    )
    for env in (export_env, drill_env):
        assert env["SIBYL_HTTP_CONNECT_TIMEOUT"] == "10"
        assert env["SIBYL_HTTP_MAX_TIME"] == "3600"

    custom, _ = _raw_env(
        "restore-drill",
        "--set",
        f"restoreDrill.backoffLimit={CUSTOM_BACKOFF}",
        "--set",
        f"restoreDrill.activeDeadlineSeconds={CUSTOM_DEADLINE_SECONDS}",
    )
    assert custom["backoffLimit"] == CUSTOM_BACKOFF
    assert custom["activeDeadlineSeconds"] == CUSTOM_DEADLINE_SECONDS


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (("--set-string", "export.filePrefix=sibyl.prod"), "export.filePrefix must match"),
        (
            ("--set-string", "jobDefaults.http.maxTimeSeconds=1h"),
            "jobDefaults.http.maxTimeSeconds must be a whole number",
        ),
        (
            ("--set", "restoreDrill.backoffLimit=-1"),
            "restoreDrill.backoffLimit must be a whole number",
        ),
        (
            ("--set", "restoreDrill.rowDrift.percent=101"),
            "restoreDrill.rowDrift.percent must be a number from 0 to 100",
        ),
        (
            ("--set-string", "restoreDrill.rowDrift.rows=two"),
            "restoreDrill.rowDrift.rows must be a whole number",
        ),
    ],
    ids=["prefix-with-dot", "timeout-text", "negative-backoff", "percent-over-100", "rows-text"],
)
def test_helm_ops_values_are_validated_at_render(override: tuple[str, str], message: str) -> None:
    assert _HELM is not None
    rendered = subprocess.run(  # noqa: S603
        [
            _HELM,
            "template",
            "drill",
            "charts/surrealdb",
            "--set",
            "export.enabled=true",
            "--set",
            "restoreDrill.enabled=true",
            *override,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode != 0
    assert message in rendered.stderr


def test_helm_restore_drill_fails_when_a_tiny_table_empties(harness: OpsHarness) -> None:
    """users exports 2 rows; the 2-row allowance must not let it restore 0."""
    _export_then_reset(harness, lambda h: h.serve(shrink_on_import={"sibyl_auth/auth/users": 0}))

    result = harness.run("restore-drill")

    assert result.returncode != 0
    assert (
        "restore failed for sibyl_auth/auth: tables short after import: users (0 of 2, emptied)"
        in result.stderr
    )


def test_helm_export_exit_codes_tell_recorded_failures_from_transient_ones(
    harness: OpsHarness,
) -> None:
    """Recorded failures exit 111, which podFailurePolicy fails at once;
    anything else keeps a normal non-zero code and is retried."""
    harness.serve(source=_source(), ns_info_errors=(ORG_B,))
    recorded = harness.run("export")
    assert recorded.returncode == RECORDED_FAILURE_EXIT, recorded.stderr
    failures = _manifest(harness)["failures"]
    assert failures == [
        {
            "namespace": ORG_B,
            "database": None,
            "reason": failures[0]["reason"],
        }
    ]
    assert failures[0]["reason"].startswith("INFO FOR NS failed: SurrealQL failed in")

    for run_dir in _run_dirs(harness):
        shutil.rmtree(run_dir)
    harness.serve()  # no source server: every request fails to connect
    transient = harness.run("export")
    assert transient.returncode not in (0, RECORDED_FAILURE_EXIT), transient.stderr


def test_helm_export_job_fails_fast_only_on_recorded_failures() -> None:
    spec, _ = _raw_env("export")
    pod = spec["template"]["spec"]
    (container,) = pod["containers"]
    assert pod["restartPolicy"] == "Never", "podFailurePolicy requires restartPolicy Never"
    assert spec["podFailurePolicy"] == {
        "rules": [
            {
                "action": "FailJob",
                "onExitCodes": {
                    "containerName": container["name"],
                    "operator": "In",
                    "values": [RECORDED_FAILURE_EXIT],
                },
            }
        ]
    }
