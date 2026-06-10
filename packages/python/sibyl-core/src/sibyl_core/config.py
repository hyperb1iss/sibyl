"""Core configuration for Sibyl graph runtimes, LLM, and embedding settings.

This module contains settings required by sibyl-core operations.
Server-specific settings (HTTP and auth middleware) remain in sibyl-server.
"""

import os
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CoreConfig(BaseSettings):
    """Core settings for graph operations, LLM, and embeddings.

    These settings are shared across sibyl-core, sibyl-cli, and sibyl-server.
    Server-specific settings are defined separately in sibyl-server.
    """

    model_config = SettingsConfigDict(
        env_prefix="SIBYL_",
        extra="ignore",
    )

    # Environment
    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Runtime environment",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Logging level",
    )
    server_name: str = Field(
        default="sibyl",
        description="Server/instance name for identification",
    )

    store: Literal["surreal"] = Field(
        default="surreal",
        description="Active persistence runtime for this process",
    )

    # SurrealDB configuration
    surreal_url: str = Field(
        default="",
        description="Explicit SurrealDB connection URL (memory://, surrealkv://, ws://, http://)",
    )
    surreal_data_dir: str = Field(
        default="",
        description="Local SurrealKV data directory when surreal_url is not provided",
    )
    surreal_username: str = Field(
        default="",
        description="SurrealDB username for remote runtimes",
    )
    surreal_password: SecretStr = Field(
        default=SecretStr(""),
        description="SurrealDB password for remote runtimes",
    )
    surreal_token: SecretStr = Field(
        default=SecretStr(""),
        description="SurrealDB bearer token for remote runtimes",
    )
    surreal_namespace_prefix: str = Field(
        default="org_",
        description="Namespace prefix for org-scoped SurrealDB data",
    )
    surreal_database: str = Field(
        default="graph",
        description="SurrealDB database name used inside each org namespace",
    )
    surreal_slow_query_ms: float = Field(
        default=500.0,
        ge=0.0,
        description="Log SurrealDB queries at warning level when elapsed time exceeds this threshold.",
    )
    surreal_pool_size: int = Field(
        default=8,
        ge=1,
        le=256,
        description="Default concurrent SurrealDB connections per dedicated client.",
    )
    surreal_auth_pool_size: int | None = Field(
        default=None,
        ge=1,
        le=256,
        description="Override SurrealDB auth client pool size; defaults to surreal_pool_size.",
    )
    surreal_content_pool_size: int | None = Field(
        default=None,
        ge=1,
        le=256,
        description="Override SurrealDB content client pool size; defaults to surreal_pool_size.",
    )
    surreal_graph_pool_size: int | None = Field(
        default=None,
        ge=1,
        le=256,
        description="Override SurrealDB graph client pool size; defaults to surreal_pool_size.",
    )
    surreal_graph_client_cache_size: int = Field(
        default=64,
        ge=1,
        description="Maximum org-scoped native graph clients kept open per process.",
    )
    allow_embedded_single_writer: bool = Field(
        default=False,
        description="Allow embedded SurrealDB storage in production for explicit single-writer mode",
    )

    # LLM Provider configuration
    llm_provider: Literal["openai", "anthropic"] = Field(
        default="anthropic",
        description="LLM provider for entity extraction",
    )
    llm_model: str = Field(
        default="claude-haiku-4-5",
        description="LLM model for entity extraction",
    )

    # Anthropic configuration
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Anthropic API key",
    )

    # OpenAI configuration (for embeddings)
    openai_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="OpenAI API key for embeddings",
    )
    gemini_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Gemini API key for Google embeddings",
    )

    # Auth primitives used by sibyl-core when the server package is absent.
    jwt_secret: SecretStr = Field(
        default=SecretStr(""),
        description="JWT signing secret",
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT signing algorithm")
    access_token_expire_minutes: int = Field(
        default=60,
        ge=5,
        le=1440,
        description="Access token TTL in minutes",
    )
    refresh_token_expire_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="Refresh token TTL in days",
    )
    password_pepper: SecretStr = Field(
        default=SecretStr(""),
        description="Optional password pepper",
    )
    password_iterations: int = Field(
        default=310_000,
        ge=100_000,
        le=2_000_000,
        description="PBKDF2-HMAC-SHA256 iterations for local passwords",
    )

    @model_validator(mode="after")
    def check_api_key_fallbacks(self) -> "CoreConfig":
        """Fall back to non-prefixed env vars for API keys."""
        if not self.anthropic_api_key.get_secret_value():
            fallback = os.environ.get("ANTHROPIC_API_KEY", "")
            if fallback:
                object.__setattr__(self, "anthropic_api_key", SecretStr(fallback))

        if not self.openai_api_key.get_secret_value():
            fallback = os.environ.get("OPENAI_API_KEY", "")
            if fallback:
                object.__setattr__(self, "openai_api_key", SecretStr(fallback))

        if not self.gemini_api_key.get_secret_value():
            fallback = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
            if fallback:
                object.__setattr__(self, "gemini_api_key", SecretStr(fallback))

        if not self.jwt_secret.get_secret_value():
            fallback = os.environ.get("JWT_SECRET", "")
            if fallback:
                object.__setattr__(self, "jwt_secret", SecretStr(fallback))

        if self.surreal_url and self.surreal_data_dir:
            raise ValueError("Configure only one of surreal_url or surreal_data_dir")

        if self.environment == "production":
            resolved = self.resolved_surreal_url
            if resolved.startswith("memory://"):
                raise ValueError(
                    "CRITICAL: In-memory SurrealDB is forbidden in production. "
                    "Set SIBYL_SURREAL_URL or SIBYL_SURREAL_DATA_DIR."
                )
            if resolved.startswith("surrealkv://") and not self.allow_embedded_single_writer:
                raise ValueError(
                    "CRITICAL: Embedded SurrealDB requires explicit single-writer opt-in in "
                    "production. Set SIBYL_ALLOW_EMBEDDED_SINGLE_WRITER=1 only when one "
                    "daemon owns the database."
                )

        return self

    # Embedding configuration
    embedding_provider: Literal["openai", "gemini"] = Field(
        default="openai",
        description="Provider for document chunk embeddings",
    )
    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="Document chunk embedding model",
    )
    embedding_dimensions: int = Field(
        default=1536,
        ge=128,
        le=3072,
        description="Document chunk embedding vector dimensions",
    )
    graph_embedding_provider: Literal["openai", "gemini"] = Field(
        default="openai",
        description="Provider for graph node and relationship embeddings",
    )
    graph_embedding_model: str = Field(
        default="text-embedding-3-small",
        description="Graph node and relationship embedding model",
    )
    graph_embedding_dimensions: int = Field(
        default=1024,
        ge=128,
        le=3072,
        description="Graph embedding dimensions; also sizes native Surreal vector indexes",
    )
    graph_embedding_timeout_seconds: float = Field(
        default=20.0,
        ge=0.0,
        le=600.0,
        description="Maximum time to wait for graph embedding provider calls; 0 disables timeout.",
    )
    graph_search_embedding_timeout_seconds: float = Field(
        default=5.0,
        ge=0.0,
        le=120.0,
        description="Maximum time to wait for graph search query embeddings; 0 disables timeout.",
    )
    graph_hnsw_efc: int = Field(
        default=150,
        ge=1,
        le=10_000,
        description="Surreal HNSW graph index EF construction value.",
    )
    graph_hnsw_m: int = Field(
        default=12,
        ge=1,
        le=512,
        description="Surreal HNSW graph index max connections per element.",
    )
    graph_knn_ef: int = Field(
        default=40,
        ge=1,
        le=10_000,
        description="Surreal KNN query effort for graph vector retrieval.",
    )

    # Retrieval: cross-encoder reranking (optional).
    # Requires the `reranking` extra (sentence-transformers); when the extra is
    # absent the path degrades to the fused order rather than raising.
    rerank_enabled: bool = Field(
        default=False,
        description=(
            "Enable cross-encoder reranking after RRF fusion. Optional: requires the "
            "`reranking` extra (sentence-transformers). Adds ~100-200ms latency; no-ops "
            "with a clean fallback when the extra is not installed."
        ),
    )
    rerank_model: str = Field(
        default="cross-encoder/ms-marco-MiniLM-L-6-v2",
        description="Cross-encoder model for reranking (requires the `reranking` extra).",
    )
    rerank_top_k: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of top candidates to rerank (rest pass through unchanged).",
    )

    # Retrieval: temporal decay
    temporal_decay_days: float = Field(
        default=365.0,
        gt=0,
        description="Default decay half-life in days for temporal boosting (1 year default).",
    )

    # Ingestion configuration
    chunk_max_tokens: int = Field(
        default=1000,
        description="Maximum tokens per chunk during ingestion",
    )
    chunk_overlap_tokens: int = Field(
        default=100,
        description="Token overlap between chunks",
    )

    @property
    def resolved_surreal_url(self) -> str:
        """Construct the effective SurrealDB connection URL."""
        if self.surreal_url:
            return self.surreal_url
        if self.surreal_data_dir:
            return f"surrealkv://{self.surreal_data_dir}"
        return "memory://"

    def surreal_client_pool_size(self, client_kind: Literal["auth", "content", "graph"]) -> int:
        """Return the configured pool size for a SurrealDB client kind."""
        override = {
            "auth": self.surreal_auth_pool_size,
            "content": self.surreal_content_pool_size,
            "graph": self.surreal_graph_pool_size,
        }[client_kind]
        return override or self.surreal_pool_size


# Default core config instance
core_config = CoreConfig()

# Alias for backwards compatibility with tools that import 'settings'
settings = core_config
