"""The 409 for a deployment-owned setting, exactly as an HTTP client receives it.

The web settings pages parse this body, and their tests load the same files
from apps/web/src/test/fixtures/api. A change to the payload fails here until
the fixture changes too, and then the web tests run against the new shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from sibyl.ai.llm import routes as llm_routes
from sibyl.api.app import create_api_app
from sibyl.api.routes import settings as settings_routes
from sibyl.services import settings as settings_module
from sibyl_core.ai.llm.config import ConfigField, LLMSurface, ResolvedLLMConfig

FIXTURES = Path(__file__).resolve().parents[2] / "web" / "src" / "test" / "fixtures" / "api"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _wire(body: dict[str, Any]) -> dict[str, Any]:
    """The body minus its per-request id."""
    assert body.pop("request_id")
    return body


def test_settings_refusal_keeps_its_field_lists_over_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings_module,
        "_deployment_environment",
        {
            "SIBYL_EMBEDDING_MODEL": "text-embedding-3-small",
            "SIBYL_EMBEDDING_DIMENSIONS": "1536",
        },
    )
    monkeypatch.setattr(settings_routes, "require_settings_owner", AsyncMock())
    monkeypatch.setattr(settings_routes, "get_settings_service", AsyncMock)
    client = TestClient(create_api_app(), raise_server_exceptions=False)

    response = client.patch(
        "/settings",
        json={"embedding_model": "text-embedding-3-large", "embedding_dimensions": 1536},
    )

    assert response.status_code == 409
    assert _wire(response.json()) == _fixture("settings-locked-by-env-409.json")


class _LockedModelSource:
    async def resolve(self, surface: LLMSurface) -> ResolvedLLMConfig:
        return ResolvedLLMConfig(
            surface=surface,
            provider=ConfigField(value="anthropic", source="db"),
            model=ConfigField(
                value="claude-haiku-5-5",
                source="env",
                locked_by_env=True,
                env_var="SIBYL_LLM_CRAWLER_MODEL",
            ),
            temperature=ConfigField(value=0.0, source="default"),
            max_tokens=ConfigField(value=None, source="default"),
            timeout_seconds=ConfigField(value=60.0, source="default"),
            api_key=ConfigField(value=None, source="default"),
        )


def test_llm_refusal_keeps_its_field_list_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_routes, "require_settings_owner", AsyncMock())
    monkeypatch.setattr(llm_routes, "get_config_source", _LockedModelSource)
    client = TestClient(create_api_app(), raise_server_exceptions=False)

    response = client.put("/settings/ai/llm/crawler", json={"model": "claude-sonnet-5-5"})

    assert response.status_code == 409
    assert _wire(response.json()) == _fixture("llm-locked-by-env-409.json")
