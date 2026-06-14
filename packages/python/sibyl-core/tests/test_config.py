"""Production-guard tests for CoreConfig embedded-store selection."""

from __future__ import annotations

import pytest

from sibyl_core.config import CoreConfig


def test_core_config_ignores_project_dotenv(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SIBYL_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("SIBYL_OPENAI_API_KEY=dotenv-openai-key\n")

    config = CoreConfig()

    assert config.openai_api_key.get_secret_value() != "dotenv-openai-key"


class TestCoreConfigEmbeddedStoreGuard:
    """CoreConfig must refuse embedded stores in production like apps/api does."""

    def test_in_memory_forbidden_in_production(self) -> None:
        with pytest.raises(ValueError, match="In-memory SurrealDB is forbidden in production"):
            CoreConfig(
                _env_file=None,
                environment="production",
                surreal_url="",
            )

    def test_surrealkv_forbidden_in_production_without_single_writer_opt_in(self) -> None:
        with pytest.raises(ValueError, match="Embedded SurrealDB requires explicit single-writer"):
            CoreConfig(
                _env_file=None,
                environment="production",
                surreal_url="",
                surreal_data_dir="/var/lib/sibyl/surreal",
            )

    def test_surrealkv_allowed_in_production_with_single_writer_opt_in(self) -> None:
        config = CoreConfig(
            _env_file=None,
            environment="production",
            surreal_url="",
            surreal_data_dir="/var/lib/sibyl/surreal",
            allow_embedded_single_writer=True,
        )

        assert config.resolved_surreal_url == "surrealkv:///var/lib/sibyl/surreal"

    def test_remote_surreal_allowed_in_production(self) -> None:
        config = CoreConfig(
            _env_file=None,
            environment="production",
            surreal_url="ws://surrealdb:8000/rpc",
        )

        assert config.resolved_surreal_url == "ws://surrealdb:8000/rpc"

    def test_embedded_allowed_outside_production(self) -> None:
        config = CoreConfig(
            _env_file=None,
            environment="development",
            surreal_url="",
        )

        assert config.resolved_surreal_url == "memory://"


def test_surreal_client_pool_size_uses_default_for_each_client_kind() -> None:
    config = CoreConfig(_env_file=None, surreal_pool_size=12)

    assert config.surreal_client_pool_size("auth") == 12
    assert config.surreal_client_pool_size("content") == 12
    assert config.surreal_client_pool_size("graph") == 12


def test_surreal_client_pool_size_prefers_client_kind_override() -> None:
    config = CoreConfig(
        _env_file=None,
        surreal_pool_size=12,
        surreal_auth_pool_size=5,
        surreal_content_pool_size=20,
        surreal_graph_pool_size=33,
    )

    assert config.surreal_client_pool_size("auth") == 5
    assert config.surreal_client_pool_size("content") == 20
    assert config.surreal_client_pool_size("graph") == 33
