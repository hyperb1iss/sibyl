"""Trusted learning collection exercises the real runner and HTTP admission boundary."""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from benchmarks.agent_tasks import coding_controller, learning_run, runner
from benchmarks.agent_tasks.manifest import ManifestError, digest, identity, load_manifest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI

from sibyl.api.routes import memory_evals
from sibyl.auth.dependencies import get_auth_context, get_current_org_role, get_current_organization
from sibyl.config import EvalIssuerSettings
from sibyl_core.auth import OrganizationRole
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.services import content_client
from sibyl_core.tasks.eval_receipts import TaskAssignment, verify_learning_evidence

pytest_plugins = ["tools.tests.test_agent_task_runner"]


@pytest.fixture
def collection(json_oracle_experiment, monkeypatch):
    manifest, freeze, output = json_oracle_experiment
    root = freeze().parent
    source = Path(coding_controller.__file__).read_bytes()
    (root / "controller.py").write_bytes(source)
    manifest["controller"] = {
        "script": {"path": "controller.py", "sha256": digest(source)},
        "args": [
            "--image",
            "sha256:" + "a" * 64,
            "--tool-timeout",
            "5",
            "--memory-mb",
            "256",
            "--docker",
            "/usr/bin/docker",
            "--docker-host",
            "unix:///run/devbox-docker/docker.sock",
        ],
    }
    manifest["controller_tools"] = ["shell"]
    manifest["controller_api_key_env"] = "OPENROUTER_API_KEY"
    manifest["tasks"][0]["split"] = "learning"
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-test-key")
    state = SimpleNamespace(
        runs=0,
        key_loads=0,
        trace_mutation=lambda records: records,
        receipt_mutation=lambda receipt: receipt,
    )

    def execute(**kwargs):
        assert kwargs["role"] == "controller"
        state.runs += 1
        request, destination = kwargs["request"], kwargs["output"]
        (destination / "controller-home").mkdir()
        usage = {"input_tokens": 2, "output_tokens": 2, "tool_calls": 0, "cost_usd": 0.0}
        records: list[dict[str, Any]] = [
            {
                "kind": "start",
                "payload": {
                    "script_sha256": digest(source),
                    "request": {key: request[key] for key in coding_controller.PINNED_FIELDS},
                },
            },
            {
                "kind": "model_request",
                "payload": {"body": {"messages": [{"content": "offline fixture"}]}},
            },
            {
                "kind": "model_response",
                "payload": {
                    "status_code": 200,
                    "raw": {"choices": [{"message": {"content": "done"}}]},
                },
            },
            {"kind": "terminal", "payload": {"reason": "stop", "exit": 0, "usage": usage}},
        ]
        for index, entry in enumerate(records):
            entry.update(
                schema_version=coding_controller.TRACE_SCHEMA_VERSION,
                index=index,
                attempt_id=request["attempt_id"],
                request_id=request["request_id"],
            )
        records = state.trace_mutation(records)
        trace = b"".join(json.dumps(record).encode() + b"\n" for record in records)
        (destination / "controller-home" / "trace.jsonl").write_bytes(trace)
        runner._write_json(
            destination / "controller-stdout.txt",
            {
                **usage,
                "model": manifest["controller_model"],
                "synthetic": False,
                "trace_sha256": digest(trace),
            },
        )
        return {"returncode": 0, "timed_out": False, "process_group_quiescent": True}

    monkeypatch.setattr(runner, "_execute", execute)
    monkeypatch.setattr(coding_controller, "_require_unprivileged_user", lambda: None)
    monkeypatch.setattr(coding_controller, "_container_user", lambda *_: "1000:1000")
    monkeypatch.setattr(
        coding_controller,
        "execute_container",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "returncode": 0,
            "stdout_base64": base64.b64encode(b"42\n").decode(),
            "stderr_base64": "",
        },
    )
    real_run = runner.run_task

    def run(*args, **kwargs):
        result = real_run(*args, **kwargs)
        changed = state.receipt_mutation(result)
        changed["receipt_sha256"] = identity(
            {key: value for key, value in changed.items() if key != "receipt_sha256"}
        )
        runner._write_json(kwargs["output"] / "receipt.json", changed)
        return changed

    monkeypatch.setattr(runner, "run_task", run)
    path = freeze()
    state.manifest = manifest
    state.freeze = freeze
    state.output = output
    state.path = path
    state.key = Ed25519PrivateKey.generate()
    state.organization_id = str(uuid4())
    state.policy = identity(learning_run.controller_policy(load_manifest(path)[0]))
    state.posted = []

    def key():
        state.key_loads += 1
        assert state.runs == 1
        return state.key

    state.arguments = {
        "task_id": "task-one",
        "arm_id": "memory",
        "output": output,
        "organization_id": state.organization_id,
        "owner_principal_id": "owner",
        "issuer_id": "oracle",
        "checkpoint": 0,
        "expected_policy_sha256": state.policy,
        "load_signing_key": key,
    }
    return state


@pytest.fixture
async def api(collection, monkeypatch):
    store = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(store, reset=True)

    @asynccontextmanager
    async def session():
        yield store

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    issuer = EvalIssuerSettings(
        issuer_id="oracle",
        organization_id=collection.organization_id,
        experiment_id=collection.manifest["experiment_id"],
        experiment_revision=identity(load_manifest(collection.path)[0].model_dump(mode="json")),
        public_key_base64=base64.b64encode(collection.key.public_key().public_bytes_raw()).decode(),
        controller_policy_sha256=collection.policy,
    )
    monkeypatch.setattr(memory_evals.settings, "eval_issuers", [issuer])
    monkeypatch.setattr(memory_evals.memory_auth, "authorize_memory_policy", AsyncMock())
    app = FastAPI()
    app.include_router(memory_evals.router, prefix="/api")
    app.dependency_overrides[get_auth_context] = lambda: SimpleNamespace(
        user_id="owner", organization_id=collection.organization_id
    )
    app.dependency_overrides[get_current_organization] = lambda: SimpleNamespace(
        id=collection.organization_id
    )
    app.dependency_overrides[get_current_org_role] = lambda: OrganizationRole.OWNER
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test/api/"
    ) as client:
        try:
            yield client
        finally:
            await store.close()


async def collect(state, client):
    return await learning_run.collect_learning_attempt(state.path, client=client, **state.arguments)


async def test_real_runner_to_http_stores_exact_signed_episode(collection, api):
    result = await collect(collection, api)
    bundle = json.loads((collection.output / "admission-bundle.json").read_bytes())
    assignment = TaskAssignment.model_validate(bundle["assignment"])
    body = bundle["request"]
    material = {
        f"{name}_bytes": base64.b64decode(body[f"{name}_base64"])
        for name in ("outcome", "transcript", "episode")
    }
    verified = verify_learning_evidence(
        base64.b64decode(body["receipt_base64"]),
        trusted_public_key=collection.key.public_key(),
        trusted_issuer_id="oracle",
        expected_assignment=assignment,
        expected_controller_policy_sha256=collection.policy,
        **material,
    )
    async with content_client.surreal_content_client() as store:
        captures = await content_client.select_many(store, "SELECT * FROM raw_captures;")
        attempts = await content_client.select_many(store, "SELECT * FROM eval_attempts;")
    assert len(captures) == len(attempts) == 1
    raw_content = captures[0]["raw_content"]
    assert isinstance(raw_content, str)
    assert raw_content.encode() == verified.episode_bytes
    assert captures[0]["uuid"] == result["memory_id"]
    assert result["revision"] == 1
    assert collection.runs == collection.key_loads == 1
    assert bundle["sealed_isolation"] is False
    assert await learning_run.retry_learning_admission(collection.output, client=api) == result
    assert collection.runs == collection.key_loads == 1


async def test_registration_failure_never_executes_or_loads_key(collection):
    async with httpx.AsyncClient(
        base_url="http://test/api/",
        transport=httpx.MockTransport(lambda request: httpx.Response(403)),
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await collect(collection, client)
    assert collection.runs == collection.key_loads == 0


@pytest.mark.parametrize("split", ["development", "sealed"])
async def test_nonlearning_refused_before_registration(collection, split):
    collection.manifest["tasks"][0]["split"] = split
    collection.freeze()
    async with httpx.AsyncClient(
        base_url="http://test/api/",
        transport=httpx.MockTransport(lambda request: pytest.fail("registration must not run")),
    ) as client:
        with pytest.raises(ManifestError, match="learning-only"):
            await collect(collection, client)
    assert collection.runs == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("budget_status", "exceeded"),
        ("status", "controller_budget_exceeded"),
        ("status", "oracle_runtime_error"),
    ],
)
async def test_bad_terminal_result_never_signs(collection, api, field, value):
    collection.receipt_mutation = lambda receipt: receipt | {field: value}
    with pytest.raises(ManifestError):
        await collect(collection, api)
    assert collection.runs == 1
    assert collection.key_loads == 0
    assert not (collection.output / "admission-bundle.json").exists()


@pytest.mark.parametrize(
    "mutation", ["wrong_attempt", "missing_terminal", "wrong_index", "unpaired_action"]
)
async def test_invalid_trace_never_signs(collection, api, mutation):
    def mutate(records):
        if mutation == "wrong_attempt":
            records[1]["attempt_id"] = "0" * 32
        elif mutation == "missing_terminal":
            records.pop()
        elif mutation == "wrong_index":
            records[1]["index"] = 4
        else:
            records[2]["kind"] = "tool_result"
        return records

    collection.trace_mutation = mutate
    with pytest.raises(ManifestError):
        await collect(collection, api)
    assert collection.key_loads == 0


async def test_lost_admission_response_retries_without_execution(collection, api, monkeypatch):
    original = api.post
    lost = False

    async def post(url, **kwargs):
        nonlocal lost
        response = await original(url, **kwargs)
        if str(url).endswith("/admit") and not lost:
            lost = True
            assert (collection.output / "admission-bundle.json").is_file()
            raise httpx.ReadError("response lost after commit")
        return response

    monkeypatch.setattr(api, "post", post)
    with pytest.raises(httpx.ReadError):
        await collect(collection, api)
    result = await learning_run.retry_learning_admission(collection.output, client=api)
    assert result["revision"] == 1
    assert collection.runs == collection.key_loads == 1
    async with content_client.surreal_content_client() as store:
        assert len(await content_client.select_many(store, "SELECT * FROM raw_captures;")) == 1


async def test_original_inputs_changed_after_registration_do_not_change_execution(
    collection, api, monkeypatch
):
    original = api.post

    async def post(url, **kwargs):
        response = await original(url, **kwargs)
        if str(url).endswith("/assignments"):
            (collection.path.parent / "prompt.txt").write_text("Changed after registration")
        return response

    monkeypatch.setattr(api, "post", post)
    await collect(collection, api)
    episode = json.loads((collection.output / "signed-episode.bin").read_bytes())
    assert episode["goal"] == "Use the supplied memory to update answer.txt."


async def test_unknown_controller_or_policy_never_registers(collection):
    collection.arguments["expected_policy_sha256"] = "0" * 64
    async with httpx.AsyncClient(
        base_url="http://test/api/",
        transport=httpx.MockTransport(lambda request: pytest.fail("registration must not run")),
    ) as client:
        with pytest.raises(ManifestError, match="authorized policy"):
            await collect(collection, client)
    assert collection.runs == 0


def test_external_attempt_id_preserves_runner_identity(experiment):
    _, freeze, output = experiment
    receipt = runner.run_task(
        freeze(), task_id="task-one", arm_id="memory", output=output, attempt_id="b" * 32
    )
    assert receipt["attempt_id"] == "b" * 32


@pytest.mark.parametrize("attempt_id", ["wrong", "A" * 32, "../" + "0" * 29])
def test_invalid_external_attempt_id_refused_before_output(experiment, attempt_id):
    _, freeze, output = experiment
    with pytest.raises(ManifestError, match="attempt_id"):
        runner.run_task(
            freeze(), task_id="task-one", arm_id="memory", output=output, attempt_id=attempt_id
        )
    assert not output.exists()


async def test_arbitrary_frozen_controller_cannot_run(collection):
    source = b"raise SystemExit(0)\n"
    (collection.path.parent / "controller.py").write_bytes(source)
    collection.manifest["controller"]["script"]["sha256"] = digest(source)
    collection.freeze()
    async with httpx.AsyncClient(
        base_url="http://test/api/",
        transport=httpx.MockTransport(lambda request: pytest.fail("registration must not run")),
    ) as client:
        with pytest.raises(ManifestError, match="installed fixed"):
            await collect(collection, client)
    assert collection.runs == collection.key_loads == 0


async def test_registration_echo_mismatch_never_executes(collection):
    def post(request):
        body = json.loads(request.content)
        return httpx.Response(200, json=body["assignment"] | {"seed": 1234})

    async with httpx.AsyncClient(
        base_url="http://test/api/", transport=httpx.MockTransport(post)
    ) as client:
        with pytest.raises(ManifestError, match="registered assignment differs"):
            await collect(collection, client)
    assert collection.runs == collection.key_loads == 0


async def test_changed_frozen_task_refused_before_execution(collection, api, monkeypatch):
    original = api.post

    async def post(url, **kwargs):
        response = await original(url, **kwargs)
        if str(url).endswith("/assignments"):
            path = collection.output / "frozen" / "inputs" / "_learning_manifest.json"
            frozen = json.loads(path.read_bytes())
            frozen["tasks"][0]["family_id"] = "replacement-family"
            path.write_text(json.dumps(frozen))
        return response

    monkeypatch.setattr(api, "post", post)
    with pytest.raises(ManifestError, match="changed before execution"):
        await collect(collection, api)
    assert collection.runs == collection.key_loads == 0


async def test_deleted_admitted_capture_cannot_be_recreated_by_retry(collection, api):
    await collect(collection, api)
    async with content_client.surreal_content_client() as store:
        await store.execute_query("DELETE raw_captures;")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await learning_run.retry_learning_admission(collection.output, client=api)
    assert exc.value.response.status_code == HTTPStatus.CONFLICT
    assert collection.runs == collection.key_loads == 1


@pytest.mark.parametrize("mutation", ["trace", "snapshot", "oracle"])
async def test_changed_retained_artifact_never_signs(collection, api, monkeypatch, mutation):
    original = runner.run_task

    def run(*args, **kwargs):
        receipt = original(*args, **kwargs)
        output = kwargs["output"]
        if mutation == "trace":
            (output / "controller-trace.jsonl").chmod(0o600)
            (output / "controller-trace.jsonl").write_bytes(b"changed")
        elif mutation == "snapshot":
            (output / "checker-workspace" / "answer.txt").write_bytes(b"changed")
        else:
            data = json.loads((output / "oracle-outcome.json").read_bytes())
            data["status"] = "task_failed"
            data["passed"] = False
            (output / "oracle-outcome.json").write_text(json.dumps(data))
        return receipt

    monkeypatch.setattr(runner, "run_task", run)
    with pytest.raises(ManifestError):
        await collect(collection, api)
    assert collection.key_loads == 0


@pytest.mark.parametrize(
    ("stdout", "returncode", "status", "expected"),
    [
        (b"41", 0, "ok", "task_failed"),
        (b"invalid", 0, "ok", "candidate_protocol_invalid"),
        (b"", 3, "ok", "candidate_failed"),
        (b"", None, "timeout", "candidate_timeout"),
    ],
)
async def test_candidate_failures_preserve_learning_status(
    collection, api, monkeypatch, stdout, returncode, status, expected
):
    monkeypatch.setattr(
        coding_controller,
        "execute_container",
        lambda *_args, **_kwargs: {
            "status": status,
            "returncode": returncode,
            "stdout_base64": base64.b64encode(stdout).decode(),
            "stderr_base64": "",
        },
    )
    await collect(collection, api)
    episode = json.loads((collection.output / "signed-episode.bin").read_bytes())
    assert episode["outcome"]["status"] == expected
    assert episode["outcome"]["passed"] is False


async def test_episode_rendering_is_deterministic_for_retained_bytes(collection, api):
    await collect(collection, api)
    assignment = TaskAssignment.model_validate_json(
        (collection.output / "assignment.json").read_bytes()
    )
    manifest, _ = load_manifest(collection.output / "frozen" / "inputs" / "_learning_manifest.json")
    material = learning_run._terminal_evidence(
        collection.output / "execution", assignment, manifest, manifest.tasks[0], manifest.arms[0]
    )
    assert material[-1] == (collection.output / "signed-episode.bin").read_bytes()
