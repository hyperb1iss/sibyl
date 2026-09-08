"""The HTTP boundary authorizes the owner before accepting signed learning evidence."""

import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, HTTPException

from sibyl.api.routes import memory_evals
from sibyl.auth.dependencies import (
    get_auth_context,
    get_current_org_role,
    get_current_organization,
)
from sibyl.config import EvalIssuerSettings
from sibyl_core.auth import OrganizationRole
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.services import content_client
from sibyl_core.tasks.eval_receipts import TaskAssignment, sign_outcome


@pytest.fixture
async def eval_api(monkeypatch):
    store = SurrealContentClient(url="memory://")
    close_store = store.close
    await bootstrap_content_schema(store, reset=True)

    @asynccontextmanager
    async def session():
        yield store

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    organization_id = str(uuid4())
    ctx = SimpleNamespace(user_id="owner", organization_id=organization_id)
    role = SimpleNamespace(value=OrganizationRole.OWNER)
    key = Ed25519PrivateKey.generate()
    assignment = TaskAssignment(
        organization_id=organization_id,
        owner_principal_id="owner",
        experiment_id="experiment",
        experiment_revision="1",
        task_id="task",
        task_revision="1",
        task_sha256="a" * 64,
        family_id="family",
        split="learning",
        arm_id="raw",
        checkpoint=0,
        seed=1,
        memory_pack_sha256="b" * 64,
        controller_policy_sha256="c" * 64,
        oracle_sha256="d" * 64,
        evaluator_sha256="e" * 64,
        runtime_sha256="f" * 64,
        checker_sha256="0" * 64,
        image="sha256:" + "1" * 64,
        attempt_id=uuid4().hex,
    )
    issuer = EvalIssuerSettings(
        issuer_id="oracle",
        organization_id=organization_id,
        experiment_id=assignment.experiment_id,
        experiment_revision=assignment.experiment_revision,
        public_key_base64=base64.b64encode(key.public_key().public_bytes_raw()).decode(),
        controller_policy_sha256=assignment.controller_policy_sha256,
    )
    monkeypatch.setattr(memory_evals.settings, "eval_issuers", [issuer])
    policy = AsyncMock()
    monkeypatch.setattr(memory_evals.memory_auth, "authorize_memory_policy", policy)
    app = FastAPI()
    app.include_router(memory_evals.router)
    app.dependency_overrides[get_auth_context] = lambda: ctx
    app.dependency_overrides[get_current_organization] = lambda: SimpleNamespace(id=organization_id)
    app.dependency_overrides[get_current_org_role] = lambda: role.value

    outcome = {
        "schema_version": "sibyl-json-cli-outcome-v1",
        "attempt_id": assignment.attempt_id,
        "snapshot_sha256": "2" * 64,
        "status": "passed",
        "passed": True,
        **{
            field: getattr(assignment, field)
            for field in (
                "oracle_sha256",
                "evaluator_sha256",
                "runtime_sha256",
                "checker_sha256",
                "image",
            )
        },
    }
    evidence = {
        "outcome_bytes": json.dumps(outcome).encode(),
        "transcript_bytes": b'{"tool":"check","exit_code":0}\n',
        "episode_bytes": b"The candidate produced the expected JSON value.",
    }
    signed = sign_outcome(assignment=assignment, issuer_id="oracle", private_key=key, **evidence)
    body = {"issuer_id": "oracle", "receipt_base64": base64.b64encode(signed).decode()}
    body.update(
        {
            name.replace("_bytes", "_base64"): base64.b64encode(value).decode()
            for name, value in evidence.items()
        }
    )
    path = f"/memory/eval/experiments/experiment/attempts/{assignment.attempt_id}/admit"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            yield SimpleNamespace(
                client=client,
                store=store,
                assignment=assignment,
                ctx=ctx,
                role=role,
                policy=policy,
                body=body,
                path=path,
            )
        finally:
            await close_store()


async def _register(api):
    return await api.client.post(
        "/memory/eval/assignments",
        json={"issuer_id": "oracle", "assignment": api.assignment.model_dump(mode="json")},
    )


async def test_http_admission_retains_one_source_on_exact_retry(eval_api):
    api = eval_api
    assert (await _register(api)).status_code == 200
    first = await api.client.post(api.path, json=api.body)
    assert first.status_code == 200, first.text
    again = await api.client.post(api.path, json=api.body)
    assert again.status_code == 200, again.text
    assert first.json() == again.json()
    records = await content_client.select_many(api.store, "SELECT * FROM raw_captures;")
    assert len(records) == 1
    assert records[0]["raw_content"] == "The candidate produced the expected JSON value."
    assert records[0]["revision"] == first.json()["revision"]


@pytest.mark.parametrize("kind", ["tenant", "owner", "role", "policy", "issuer"])
async def test_registration_requires_private_scope_and_server_trust(eval_api, kind, monkeypatch):
    api = eval_api
    if kind == "tenant":
        api.ctx.organization_id = "other"
    elif kind == "owner":
        api.ctx.user_id = "other"
    elif kind == "role":
        api.role.value = OrganizationRole.MEMBER
    elif kind == "policy":
        api.policy.side_effect = HTTPException(status_code=403, detail="scope denied")
    else:
        monkeypatch.setattr(memory_evals.settings, "eval_issuers", [])
    assert (await _register(api)).status_code == 403
    assert await content_client.select_many(api.store, "SELECT * FROM eval_attempts;") == []


@pytest.mark.parametrize("kind", ["owner", "issuer", "base64", "tamper"])
async def test_admission_rejects_untrusted_or_changed_evidence(eval_api, kind, monkeypatch):
    api = eval_api
    assert (await _register(api)).status_code == 200
    if kind == "owner":
        api.ctx.user_id = "other"
        expected = 403
    elif kind == "issuer":
        monkeypatch.setattr(memory_evals.settings, "eval_issuers", [])
        expected = 403
    elif kind == "base64":
        api.body["episode_base64"] = "!"
        expected = 400
    else:
        api.body["episode_base64"] = base64.b64encode(b"fabricated lesson").decode()
        expected = 400
    result = await api.client.post(api.path, json=api.body)
    assert result.status_code == expected, result.text
    assert await content_client.select_many(api.store, "SELECT * FROM raw_captures;") == []


@pytest.mark.parametrize("purged", [False, True])
async def test_archive_restore_preserves_consumed_attempt(eval_api, monkeypatch, purged):
    from sibyl.persistence import content_archive

    api = eval_api
    assert (await _register(api)).status_code == 200
    admitted = await api.client.post(api.path, json=api.body)
    assert admitted.status_code == 200
    if purged:
        await api.store.execute_query("DELETE FROM raw_captures;")
    else:
        await api.store.execute_query("UPDATE raw_captures SET deleted_at = time::now();")
    original_close = api.store.close
    monkeypatch.setattr(api.store, "close", AsyncMock())
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: api.store)
    payload = await content_archive.export_content_archive_payload(api.assignment.organization_id)
    assert len(payload["tables"].get("eval_attempts", [])) == 1

    restored = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(restored, reset=True)
    restored_close = restored.close
    monkeypatch.setattr(restored, "close", AsyncMock())
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: restored)

    @asynccontextmanager
    async def restored_session():
        yield restored

    monkeypatch.setattr(content_client, "surreal_content_client", restored_session)
    try:
        result = await content_archive.restore_content_archive_payload(payload)
        assert not result.errors
        assert (await _register(api)).status_code == 200
        replay = await api.client.post(api.path, json=api.body)
        assert replay.status_code == 409, replay.text
        captures = await content_client.select_many(restored, "SELECT * FROM raw_captures;")
        assert len(captures) == (0 if purged else 1)
        if captures:
            assert captures[0]["deleted_at"] is not None
    finally:
        await restored_close()
        await original_close()
