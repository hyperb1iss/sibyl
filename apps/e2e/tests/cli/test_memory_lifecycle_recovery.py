"""Complete public lifecycle runs with native restart and committed ACK loss."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tests.cli.lifecycle_runtime import LifecycleRuntime, OwnedCLI


@pytest.fixture
def lifecycle_runtime(tmp_path: Path, request) -> Iterator[LifecycleRuntime]:
    receipt_root = os.environ.get("SIBYL_LIFECYCLE_RECEIPT_DIR")
    directory = (
        Path(receipt_root) / f"{request.node.callspec.id}-{uuid4().hex}"
        if receipt_root
        else tmp_path
    )
    runtime = LifecycleRuntime(directory)
    try:
        runtime.start()
        yield runtime
    finally:
        runtime.close()


@pytest.fixture(autouse=True)
def require_services(lifecycle_runtime: LifecycleRuntime) -> None:
    """Use only the owned daemon, never the shared E2E service prerequisite."""


def require_json(result) -> dict:
    assert result.success, result.stdout + result.stderr
    value = result.json()
    assert isinstance(value, dict), value
    return value


def raw_ids(payload: dict) -> set[str]:
    return {str(row["id"]) for row in payload["memories"]}


@pytest.mark.cli
@pytest.mark.parametrize(
    ("scope", "fault_phase", "iteration"),
    [
        (scope, phase, iteration)
        for scope in ("private", "project")
        for phase, count in (("capture", 4), ("revise", 2), ("delete", 2), ("restore", 2))
        for iteration in range(1, count + 1)
    ],
    ids=[
        f"{scope}-{phase}-{iteration}"
        for scope in ("private", "project")
        for phase, count in (("capture", 4), ("revise", 2), ("delete", 2), ("restore", 2))
        for iteration in range(1, count + 1)
    ],
)
def test_capture_correction_delete_restore_recall_after_restart(
    lifecycle_runtime: LifecycleRuntime,
    scope: str,
    fault_phase: str,
    iteration: int,
) -> None:
    runtime = lifecycle_runtime
    marker = f"lifecycle-{scope}-{fault_phase}-{iteration}-{uuid4().hex}"
    original = f"{marker} original routing instruction."
    revised = f"{marker} corrected routing instruction."
    source_key = f"acceptance:{marker}"
    with httpx.Client(base_url=runtime.proxy_url, timeout=60, trust_env=False) as client:
        signup = client.post(
            "/auth/local/signup",
            json={
                "email": f"{marker}@example.com",
                "password": "lifecycle-safe-password-42",
                "name": "Lifecycle acceptance",
            },
        )
        assert signup.status_code == 201, signup.text
        identity = signup.json()
        token = identity["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        cli = OwnedCLI(runtime, token)
        project = require_json(cli.project_create(marker))["id"]
        capture_args = [
            "remember",
            marker,
            original,
            "--raw",
            "--source-id",
            source_key,
            "--scope",
            scope,
            "--json",
            "--all-projects",
        ]
        scope_args = ["--scope", scope]
        if scope == "project":
            capture_args += ["--project", project, "--scope-key", project]
            scope_args += ["--scope-key", project, "--project", project]
        else:
            scope_args += ["--all"]
        capture_path = "/api/memory/raw"

        def invoke_with_loss(args: list[str], path: str) -> dict:
            runtime.drop_path = path
            result = cli.run(*args)
            assert not result.success, result.stdout + result.stderr
            writes = runtime.pending()
            assert len(writes) == 1, writes
            pending = writes[0]
            committed = [
                row for row in runtime.requests if row.get("ack_dropped_after_applied_receipt")
            ][-1]
            receipt = committed["response"]["mutation_receipt"]
            assert pending["idempotency_key"] == committed["key"] == receipt["operation_id"]
            assert receipt["applied"] is True
            runtime.record("pending_before_restart", pending)
            old_pid = runtime.process.pid
            runtime.restart()
            assert runtime.process.pid != old_pid
            flushed = cli.run("pending-writes", "flush", pending["id"])
            assert flushed.success, flushed.stdout + flushed.stderr
            assert runtime.pending() == []
            responses = [
                row
                for row in runtime.requests
                if row["method"] == "POST"
                and row["path"] == path
                and row["key"] == receipt["idempotency_key"]
            ]
            assert len(responses) == 2, responses
            replay = responses[-1]["response"]
            assert replay["mutation_receipt"] == {**receipt, "replayed": True}, replay
            runtime.record(
                "replay_verified",
                {
                    "operation_id": receipt["operation_id"],
                    "responses": responses,
                },
            )
            return replay

        capture = (
            invoke_with_loss(capture_args, capture_path)
            if fault_phase == "capture"
            else require_json(cli.run(*capture_args))
        )
        source_id = capture["id"]
        capture_receipt = capture["mutation_receipt"]
        assert capture_receipt["applied"] is True
        inspected = client.get(f"/memory/inspect/{source_id}")
        assert inspected.status_code == 200, inspected.text
        before = inspected.json()
        assert before["raw_content"] == original
        assert before["source_id"] == source_key
        assert before["revision"] == capture_receipt["revision"]
        assert before["capture_surface"] == "cli"
        canonical = require_json(cli.run("context", marker, "--raw", "--json", *scope_args))
        assert raw_ids(canonical) == {source_id}, canonical
        audit = client.get(
            "/memory/audit", params={"action": "memory.remember", "source_id": source_key}
        )
        assert audit.status_code == 200, audit.text
        assert len(audit.json()["events"]) == 1, audit.text
        count = client.post(
            "/admin/debug/query",
            json={
                "cypher": "SELECT count() AS total FROM raw_captures WHERE source_id = $source GROUP ALL;",
                "params": {"source": source_key},
            },
        )
        assert count.status_code == 200, count.text
        assert not count.json().get("error"), count.text
        assert count.json()["rows"] == [{"total": 1}], count.text
        runtime.record("canonical_count_verified", count.json())

        control = require_json(
            cli.run(
                "remember",
                marker + " control",
                marker + " independent control.",
                "--raw",
                "--source-id",
                source_key + ":control",
                "--scope",
                scope,
                "--json",
                "--all-projects",
                *(["--project", project, "--scope-key", project] if scope == "project" else []),
            )
        )
        control_id = control["id"]
        control_before = client.get(f"/memory/inspect/{control_id}").json()

        def create_node(name: str, metadata: dict, related_to: list[str] | None = None) -> str:
            response = client.post(
                "/entities",
                params={"sync": "true"},
                json={
                    "name": name,
                    "content": name,
                    "entity_type": "episode",
                    "metadata": metadata,
                    "related_to": related_to,
                    "skip_conflicts": True,
                    "defer_embeddings": True,
                },
            )
            assert response.status_code == 201, response.text
            return str(response.json()["id"])

        owner_metadata = {"memory_scope": scope, "principal_id": before["principal_id"]}
        if scope == "project":
            owner_metadata.update({"scope_key": project, "project_id": project})
        target_id = create_node(marker + " target", owner_metadata)
        promoted = client.post(
            "/memory/promote",
            json={
                "candidate_id": source_id,
                "promote_to_scope": scope,
                "promote_to_scope_key": project if scope == "project" else None,
                "project": project if scope == "project" else None,
                "related_to": [target_id],
            },
        )
        assert promoted.status_code == 200, promoted.text
        promotion = promoted.json()
        assert promotion["success"] is True, promotion
        assert promotion["raw_source_ids"] == [source_id], promotion
        stale_id = promotion["promoted_id"]
        stored = client.post(
            "/admin/debug/query",
            json={
                "cypher": "SELECT uuid, attributes FROM entity WHERE uuid = $target;",
                "params": {"target": stale_id},
            },
        )
        assert stored.status_code == 200, stored.text
        assert not stored.json().get("error"), stored.text
        stored_rows = stored.json()["rows"]
        assert len(stored_rows) == 1, stored.text
        bindings = stored_rows[0]["attributes"]["source_bindings"]
        assert set(bindings) == {source_id}, stored.text
        bound_revision = bindings[source_id]
        assert type(bound_revision) is int, stored.text
        assert bound_revision >= before["revision"], stored.text
        runtime.record(
            "server_source_binding_verified",
            {
                "promotion": promotion,
                "stored": stored.json(),
            },
        )
        before_promotion = before
        before = client.get(f"/memory/inspect/{source_id}").json()
        assert before["raw_content"] == original
        assert bound_revision <= before["revision"]
        assert before["provenance"] == before_promotion["provenance"]
        control_graph = create_node(
            marker + " independent graph control", owner_metadata, [target_id]
        )
        nodes_before = client.get("/graph/nodes")
        assert nodes_before.status_code == 200, nodes_before.text
        assert {stale_id, control_graph, target_id} <= {row["id"] for row in nodes_before.json()}, (
            nodes_before.text
        )
        edges_before = client.get("/graph/edges").json()
        assert any(
            row["source"] == stale_id and row["target"] == target_id for row in edges_before
        ), edges_before
        assert any(
            row["source"] == control_graph and row["target"] == target_id for row in edges_before
        ), edges_before

        revision = before["revision"]
        saved_revise_entry = None
        saved_content_epoch = None
        for action in ("revise", "delete", "restore"):
            args = [
                "correct",
                source_id,
                "--action",
                action,
                "--reason",
                marker,
                "--expected-revision",
                str(revision),
                "--json",
            ]
            if action == "revise":
                args += ["--content", revised]
            if action == "delete":
                args += ["--yes"]
            path = f"/api/memory/inspect/{source_id}/corrections"
            outcome = (
                invoke_with_loss(args, path)
                if fault_phase == action
                else require_json(cli.run(*args))
            )
            receipt = outcome["mutation_receipt"]
            assert outcome["applied"] is True, outcome
            assert receipt["applied"] is True, outcome
            assert receipt["revision"] > revision, outcome
            revision = receipt["revision"]
            current = client.get(f"/memory/inspect/{source_id}")
            assert current.status_code == 200, current.text
            detail = current.json()
            assert detail["revision"] == revision
            persisted = client.post(
                "/admin/debug/query",
                json={
                    "cypher": (
                        "SELECT uuid, revision, raw_content, metadata.correction_history AS corrections "
                        "FROM raw_captures WHERE source_id = $source;"
                    ),
                    "params": {"source": source_key},
                },
            )
            assert persisted.status_code == 200, persisted.text
            assert not persisted.json().get("error"), persisted.text
            canonical_rows = persisted.json()["rows"]
            assert len(canonical_rows) == 1, persisted.text
            canonical_row = canonical_rows[0]
            assert canonical_row["uuid"] == source_id, canonical_row
            assert canonical_row["revision"] == revision, canonical_row
            assert canonical_row["raw_content"] == revised, canonical_row
            revise_entries = [
                row for row in canonical_row["corrections"] if row["action"] == "revise"
            ]
            assert len(revise_entries) == 1, canonical_row
            content_epoch = revise_entries[0]["prior_revision"] + 1
            if action == "revise":
                saved_revise_entry = revise_entries[0]
                saved_content_epoch = content_epoch
                assert content_epoch == revision, canonical_row
            assert revise_entries[0] == saved_revise_entry, canonical_row
            assert content_epoch == saved_content_epoch, canonical_row
            assert bound_revision < content_epoch <= revision, canonical_row
            runtime.record(action + "_canonical_generation_verified", canonical_row)
            retained = client.post(
                "/admin/debug/query",
                json={
                    "cypher": "SELECT uuid, attributes FROM entity WHERE uuid = $target;",
                    "params": {"target": stale_id},
                },
            )
            assert retained.status_code == 200, retained.text
            assert retained.json()["rows"][0]["attributes"]["source_bindings"] == bindings
            assert detail["source_id"] == source_key
            assert detail["provenance"] == before["provenance"]
            assert detail["created_at"] == before["created_at"]
            assert detail["captured_at"] == before["captured_at"]
            recall = require_json(cli.run("context", marker, "--raw", "--json", *scope_args))
            expected_ids = {control_id} if action == "delete" else {source_id, control_id}
            assert raw_ids(recall) == expected_ids, recall
            if action != "delete":
                row = next(row for row in recall["memories"] if row["id"] == source_id)
                assert row["raw_content"] == revised, row
            edges = client.get("/graph/edges")
            assert edges.status_code == 200, edges.text
            assert not any(
                row["source"] == stale_id or row["target"] == stale_id for row in edges.json()
            ), edges.text
            assert any(
                row["source"] == control_graph and row["target"] == target_id
                for row in edges.json()
            ), edges.text
            nodes = client.get("/graph/nodes")
            assert nodes.status_code == 200, nodes.text
            assert stale_id not in {row["id"] for row in nodes.json()}, nodes.text
            runtime.record(
                action + "_verified",
                {
                    "receipt": receipt,
                    "source": detail,
                    "recall": recall,
                    "edges": edges.json(),
                    "nodes": nodes.json(),
                },
            )

        blame = require_json(cli.run("correct", source_id, "--json"))
        assert blame["content_revisions"][0]["content"] == original, blame
        history = blame["source"]["correction_history"]
        assert [row["action"] for row in history if "audit_event_id" not in row] == [
            "revise",
            "delete",
            "restore",
        ], blame
        for action in ("revise", "delete", "restore"):
            applied_events = [
                row for row in history if row["action"] == f"memory.correction.{action}"
            ]
            assert len(applied_events) == 1, history
        control_after = client.get(f"/memory/inspect/{control_id}")
        assert control_after.status_code == 200, control_after.text
        control_detail = control_after.json()
        for field in (
            "id",
            "source_id",
            "revision",
            "raw_content",
            "metadata",
            "provenance",
            "created_at",
            "captured_at",
            "correction_history",
            "review_state",
        ):
            assert control_detail[field] == control_before[field], (field, control_detail)
        for field in ("state", "flags", "action"):
            assert control_detail["lifecycle"][field] == control_before["lifecycle"][field]
        assert runtime.pending() == []
        assert not (runtime.home / ".cache/huggingface").exists()
        assert "graph_embeddings_disabled" in runtime.log_path.read_text()
        assert "missing_key" in runtime.log_path.read_text()
        runtime.record(
            "complete_lifecycle",
            {
                "scope": scope,
                "fault_phase": fault_phase,
                "source_id": source_id,
                "control_id": control_id,
                "revision": revision,
            },
        )
