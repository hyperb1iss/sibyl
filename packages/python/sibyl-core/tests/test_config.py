"""Production-guard tests for CoreConfig embedded-store selection."""

from __future__ import annotations

import pytest

from sibyl_core.config import CoreConfig


def test_core_config_uses_minilm_defaults_for_local_graph_embeddings() -> None:
    config = CoreConfig(_env_file=None, graph_embedding_provider="local")

    assert config.graph_embedding_model == "sentence-transformers/all-MiniLM-L6-v2"
    assert config.graph_embedding_dimensions == 384


def test_core_config_preserves_explicit_local_graph_embedding_dimensions() -> None:
    config = CoreConfig(
        _env_file=None,
        graph_embedding_provider="local",
        graph_embedding_model="BAAI/bge-m3",
        graph_embedding_dimensions=1024,
    )

    assert config.graph_embedding_model == "BAAI/bge-m3"
    assert config.graph_embedding_dimensions == 1024


def test_core_config_preserves_explicit_local_dimension_mismatch() -> None:
    config = CoreConfig(
        _env_file=None,
        graph_embedding_provider="local",
        graph_embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        graph_embedding_dimensions=1024,
    )

    assert config.graph_embedding_dimensions == 1024


def test_core_config_ignores_project_dotenv(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SIBYL_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("SIBYL_OPENAI_API_KEY=dotenv-openai-key\n")

    config = CoreConfig()

    assert config.openai_api_key.get_secret_value() != "dotenv-openai-key"


class TestCoreConfigEmbeddedStoreGuard:
    """Core refuses embedded stores in production like apps/api does.

    The refusal happens where a store is opened, not when the config loads:
    the client CLI imports CoreConfig, and a server URL it never opens must
    not stop it from starting.
    """

    def test_in_memory_forbidden_in_production(self) -> None:
        config = CoreConfig(_env_file=None, environment="production", surreal_url="")

        with pytest.raises(ValueError, match="In-memory SurrealDB is forbidden in production"):
            config.require_serviceable_surreal_url()

    def test_surrealkv_forbidden_in_production_without_single_writer_opt_in(self) -> None:
        config = CoreConfig(
            _env_file=None,
            environment="production",
            surreal_url="",
            surreal_data_dir="/var/lib/sibyl/surreal",
        )

        with pytest.raises(ValueError, match="Embedded SurrealDB requires explicit single-writer"):
            config.require_serviceable_surreal_url()

    def test_surrealkv_allowed_in_production_with_single_writer_opt_in(self) -> None:
        config = CoreConfig(
            _env_file=None,
            environment="production",
            surreal_url="",
            surreal_data_dir="/var/lib/sibyl/surreal",
            allow_embedded_single_writer=True,
        )

        assert config.require_serviceable_surreal_url() == "surrealkv:///var/lib/sibyl/surreal"

    def test_remote_surreal_allowed_in_production(self) -> None:
        config = CoreConfig(
            _env_file=None,
            environment="production",
            surreal_url="ws://surrealdb:8000/rpc",
        )

        assert config.require_serviceable_surreal_url() == "ws://surrealdb:8000/rpc"

    def test_embedded_allowed_outside_production(self) -> None:
        config = CoreConfig(
            _env_file=None,
            environment="development",
            surreal_url="",
        )

        assert config.require_serviceable_surreal_url() == "memory://"

    def test_url_and_data_dir_together_are_refused_where_the_store_opens(self) -> None:
        config = CoreConfig(
            _env_file=None,
            surreal_url="ws://surrealdb:8000/rpc",
            surreal_data_dir="/var/lib/sibyl/surreal",
        )

        with pytest.raises(ValueError, match="Configure only one of surreal_url"):
            config.require_serviceable_surreal_url()


def test_validation_errors_never_print_a_secret() -> None:
    secret = "sk-live-core-0123456789abcdefsecret"
    with pytest.raises(ValueError) as caught:
        # pydantic abbreviates the input it prints to its head and tail, so
        # the secret goes last, where an unhidden error would show it.
        CoreConfig(
            _env_file=None,
            embedding_provider="bedrock",
            embedding_dimensions=777,
            openai_api_key=secret,
        )

    message = str(caught.value)
    assert secret not in message
    assert "input_value" not in message
    # The operator still sees what was wrong and how to fix it.
    assert "SIBYL_EMBEDDING_DIMENSIONS=777" in message


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
