from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest
from benchmarks import longmemeval_v2_official as official
from benchmarks.longmemeval_v2_memory import sibyl_memory
from tools.tests.longmemeval_v2_release_support import trajectory, write_dataset


def _saved_memory(
    root: Path,
    *,
    domain: str = "web",
    project_id: str = "project-web",
    run_id: str = "memory-web",
) -> dict[str, object]:
    root.mkdir()
    config: dict[str, object] = {
        "memory_type": "sibyl_live_api",
        "memory_params": {
            "api_url": "http://127.0.0.1:3334/api",
            "allow_localhost": True,
            "longmemeval_v2_domain": domain,
            "project_id": project_id,
            "run_id": run_id,
            "content_max_chars": 18_000,
            "chunking_mode": "state",
            "include_screenshot_refs": False,
        },
    }
    (root / "memory_config.json").write_text(
        json.dumps(config) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": sibyl_memory.MEMORY_MANIFEST_SCHEMA_VERSION,
        "ingest_finalized": True,
        "longmemeval_v2_domain": domain,
        "project_id": project_id,
        "run_id": run_id,
    }
    (root / "memory_manifest.json").write_text(
        json.dumps(manifest) + "\n",
        encoding="utf-8",
    )
    return config


def _attestation_args(memory_dir: Path, *, domain: str = "web") -> Namespace:
    return Namespace(
        domain=domain,
        load_memory_dir=str(memory_dir),
        checkpoint_dir=None,
    )


def test_official_plan_only_calls_remote_attestation_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "data"
    memory_root = tmp_path / "memory"
    output_root = tmp_path / "output"
    write_dataset(data_root)
    _saved_memory(memory_root)
    calls: list[dict[str, Any]] = []

    def attest(**kwargs: Any) -> dict[str, object]:
        calls.append(kwargs)
        return {"status": "verified"}

    monkeypatch.setattr(official, "attest_loaded_memory_project", attest)
    monkeypatch.setattr(
        official,
        "resolve_official_repo",
        lambda *_args: pytest.fail("plan-only execution reached the paid harness boundary"),
    )

    result = official.main(
        [
            "--data-root",
            str(data_root),
            "--domain",
            "web",
            "--output-dir",
            str(output_root),
            "--plan-only",
            "--load-memory-dir",
            str(memory_root),
            "--allow-localhost",
        ]
    )

    assert result == 0
    assert len(calls) == 1
    assert calls[0]["selected_haystack"] == {"web-1": ["web-t1"]}
    assert (output_root / "longmemeval_v2_official_plan.json").is_file()


def test_official_attestation_binds_manifest_and_selected_trajectories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "data"
    memory_root = tmp_path / "memory"
    write_dataset(data_root)
    config = _saved_memory(memory_root)
    captured: dict[str, Any] = {}

    def attest(
        params: dict[str, object],
        *,
        expected_trajectory_ids: set[str],
        trajectories: list[dict[str, object]],
    ) -> dict[str, object]:
        captured.update(
            params=params,
            expected_trajectory_ids=expected_trajectory_ids,
            trajectories=trajectories,
        )
        return {
            "project_id": "project-web",
            "run_id": "memory-web",
            "longmemeval_v2_domain": "web",
            "content_audit": {"status": "verified"},
        }

    monkeypatch.setattr(sibyl_memory.SibylLiveApiMemory, "attest_existing", attest)

    receipt = official.attest_loaded_memory_project(
        args=_attestation_args(memory_root),
        data_root=data_root,
        selected_haystack={"web-1": ["web-t1"]},
        memory_config=config,
    )

    assert receipt is not None
    assert captured["expected_trajectory_ids"] == {"web-t1"}
    assert [row["id"] for row in captured["trajectories"]] == ["web-t1"]
    assert captured["params"]["longmemeval_v2_domain"] == "web"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("longmemeval_v2_domain", "enterprise"),
        ("project_id", "project-elsewhere"),
        ("run_id", "memory-elsewhere"),
        ("ingest_finalized", False),
    ],
)
def test_official_attestation_rejects_saved_manifest_drift(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    data_root = tmp_path / "data"
    memory_root = tmp_path / "memory"
    write_dataset(data_root)
    config = _saved_memory(memory_root)
    manifest_path = memory_root / "memory_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="manifest identity"):
        official.attest_loaded_memory_project(
            args=_attestation_args(memory_root),
            data_root=data_root,
            selected_haystack={"web-1": ["web-t1"]},
            memory_config=config,
        )


def _remote_responses(
    *,
    project_domain: str = "web",
    session_domain: str = "web",
) -> tuple[list[dict[str, object]], Any]:
    source = trajectory("web")
    payloads = sibyl_memory.build_entity_payloads_for_trajectory(
        source,
        project_id="project-web",
        run_id="memory-web",
    )
    stored: list[dict[str, object]] = []
    for index, payload in enumerate(payloads):
        metadata = payload.get("metadata")
        assert isinstance(metadata, dict)
        stored.append(
            {
                **payload,
                "id": f"session-{index}",
                "metadata": {
                    **metadata,
                    "longmemeval_v2_domain": session_domain,
                },
            }
        )

    def request(
        _self: object,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        assert method == "GET"
        if path == "/health":
            return {"status": "healthy"}
        if path == "/entities/project-web":
            return {
                "id": "project-web",
                "entity_type": "project",
                "metadata": {
                    "longmemeval_v2_run_id": "memory-web",
                    "longmemeval_v2_domain": project_domain,
                },
            }
        if path == "/entities":
            return {"entities": stored, "has_more": False}
        for row in stored:
            if path == f"/entities/{row['id']}":
                return row
        raise AssertionError(f"unexpected path {path}")

    return stored, request


def test_adapter_attestation_is_get_only_and_content_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stored, request = _remote_responses()
    methods: list[str] = []

    def tracked_request(*args: Any, **kwargs: Any) -> dict[str, object]:
        methods.append(str(args[1]))
        return request(*args, **kwargs)

    monkeypatch.setenv("SIBYL_API_TOKEN", "test-token")
    monkeypatch.setattr(sibyl_memory.SibylLiveApiMemory, "_request_json", tracked_request)

    receipt = sibyl_memory.SibylLiveApiMemory.attest_existing(
        {
            "api_url": "http://127.0.0.1:3334/api",
            "allow_localhost": True,
            "longmemeval_v2_domain": "web",
            "project_id": "project-web",
            "run_id": "memory-web",
        },
        expected_trajectory_ids={"web-t1"},
        trajectories=[trajectory("web")],
    )

    assert methods
    assert set(methods) == {"GET"}
    assert receipt["longmemeval_v2_domain"] == "web"
    content_audit = receipt["content_audit"]
    assert isinstance(content_audit, dict)
    assert content_audit["status"] == "verified"


@pytest.mark.parametrize(
    ("project_domain", "session_domain", "message"),
    [
        ("enterprise", "web", "project metadata mismatch"),
        ("web", "enterprise", "foreign domain session"),
    ],
)
def test_adapter_attestation_rejects_remote_domain_drift(
    monkeypatch: pytest.MonkeyPatch,
    project_domain: str,
    session_domain: str,
    message: str,
) -> None:
    _stored, request = _remote_responses(
        project_domain=project_domain,
        session_domain=session_domain,
    )
    monkeypatch.setenv("SIBYL_API_TOKEN", "test-token")
    monkeypatch.setattr(sibyl_memory.SibylLiveApiMemory, "_request_json", request)

    with pytest.raises(RuntimeError, match=message):
        sibyl_memory.SibylLiveApiMemory.attest_existing(
            {
                "api_url": "http://127.0.0.1:3334/api",
                "allow_localhost": True,
                "longmemeval_v2_domain": "web",
                "project_id": "project-web",
                "run_id": "memory-web",
            },
            expected_trajectory_ids={"web-t1"},
            trajectories=[trajectory("web")],
        )


def test_adapter_read_only_attestation_refuses_credential_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("SIBYL_API_TOKEN", "SIBYL_API_CREDENTIALS_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sibyl_memory, "_load_cli_auth", lambda _url: {})
    monkeypatch.setattr(
        sibyl_memory.SibylLiveApiMemory,
        "_login_or_signup",
        lambda *_args, **_kwargs: pytest.fail("attestation attempted login or signup"),
    )

    with pytest.raises(RuntimeError, match="requires existing token credentials"):
        sibyl_memory.SibylLiveApiMemory.attest_existing(
            {
                "api_url": "http://127.0.0.1:3334/api",
                "allow_localhost": True,
                "longmemeval_v2_domain": "web",
                "project_id": "project-web",
                "run_id": "memory-web",
            },
            expected_trajectory_ids={"web-t1"},
            trajectories=[trajectory("web")],
        )
