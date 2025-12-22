"""Configuration management for Sibyl MCP Server."""

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="SIBYL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server configuration
    server_name: str = Field(default="sibyl", description="MCP server name")
    server_host: str = Field(default="localhost", description="Server bind host")
    server_port: int = Field(default=3334, description="Server bind port")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Logging level",
    )

    # Auth configuration
    jwt_secret: SecretStr = Field(
        default=SecretStr(""),
        description="JWT signing secret (required for auth)",
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT signing algorithm")
    jwt_expiry_hours: int = Field(default=24, ge=1, le=720, description="Access token TTL (hours)")

    github_client_id: SecretStr = Field(default=SecretStr(""), description="GitHub OAuth client id")
    github_client_secret: SecretStr = Field(
        default=SecretStr(""), description="GitHub OAuth client secret"
    )

    server_url: str = Field(
        default="http://localhost:3334",
        description="Public base URL for this server (used for OAuth redirects)",
    )
    frontend_url: str = Field(
        default="http://localhost:3337/",
        description="Frontend base URL for auth redirects",
    )

    cookie_domain: str | None = Field(
        default=None,
        description="Cookie domain override (optional; defaults to host-only cookies)",
    )
    cookie_secure: bool | None = Field(
        default=None,
        description="Force Secure cookies on/off (default: auto based on server_url https)",
    )

    password_pepper: SecretStr = Field(
        default=SecretStr(""),
        description="Optional password pepper to harden hash storage (recommended in prod)",
    )
    password_iterations: int = Field(
        default=310_000,
        ge=100_000,
        le=2_000_000,
        description="PBKDF2-HMAC-SHA256 iterations for local passwords",
    )

    mcp_auth_mode: Literal["auto", "on", "off"] = Field(
        default="auto",
        description=("Require Bearer auth for MCP endpoints. auto=enforce when JWT secret is set."),
    )

    # FalkorDB configuration
    falkordb_host: str = Field(default="localhost", description="FalkorDB host")
    falkordb_port: int = Field(default=6380, description="FalkorDB port")
    falkordb_password: str = Field(default="conventions", description="FalkorDB password")
    falkordb_graph_name: str = Field(
        default="conventions",
        description="Name of the graph in FalkorDB",
    )
    redis_jobs_db: int = Field(
        default=1,
        description="Redis database number for job queue (0 is graph data)",
    )

    # PostgreSQL configuration
    postgres_host: str = Field(default="localhost", description="PostgreSQL host")
    postgres_port: int = Field(default=5433, description="PostgreSQL port")
    postgres_user: str = Field(default="sibyl", description="PostgreSQL user")
    postgres_password: SecretStr = Field(
        default=SecretStr("sibyl_dev"), description="PostgreSQL password"
    )
    postgres_db: str = Field(default="sibyl", description="PostgreSQL database name")
    postgres_pool_size: int = Field(default=10, description="Connection pool size")
    postgres_max_overflow: int = Field(default=20, description="Max overflow connections")

    # LLM Provider configuration
    llm_provider: Literal["openai", "anthropic"] = Field(
        default="anthropic",
        description="LLM provider for entity extraction (openai or anthropic)",
    )
    llm_model: str = Field(
        default="claude-haiku-4-5",
        description="LLM model for entity extraction",
    )

    # Anthropic configuration (SIBYL_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY)
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Anthropic API key",
    )

    # OpenAI configuration (SIBYL_OPENAI_API_KEY or OPENAI_API_KEY)
    openai_api_key: SecretStr = Field(
        default=SecretStr(""), description="OpenAI API key for embeddings"
    )

    @model_validator(mode="after")
    def check_api_key_fallbacks(self) -> "Settings":
        """Fall back to non-prefixed env vars for API keys."""
        # Anthropic: check ANTHROPIC_API_KEY if SIBYL_ANTHROPIC_API_KEY not set
        if not self.anthropic_api_key.get_secret_value():
            fallback = os.environ.get("ANTHROPIC_API_KEY", "")
            if fallback:
                object.__setattr__(self, "anthropic_api_key", SecretStr(fallback))

        # OpenAI: check OPENAI_API_KEY if SIBYL_OPENAI_API_KEY not set
        if not self.openai_api_key.get_secret_value():
            fallback = os.environ.get("OPENAI_API_KEY", "")
            if fallback:
                object.__setattr__(self, "openai_api_key", SecretStr(fallback))

        # GitHub OAuth: fall back to non-prefixed env vars
        if not self.github_client_id.get_secret_value():
            fallback = os.environ.get("GITHUB_CLIENT_ID", "")
            if fallback:
                object.__setattr__(self, "github_client_id", SecretStr(fallback))

        if not self.github_client_secret.get_secret_value():
            fallback = os.environ.get("GITHUB_CLIENT_SECRET", "")
            if fallback:
                object.__setattr__(self, "github_client_secret", SecretStr(fallback))

        # JWT: fall back to non-prefixed env vars
        if not self.jwt_secret.get_secret_value():
            fallback = os.environ.get("JWT_SECRET", "")
            if fallback:
                object.__setattr__(self, "jwt_secret", SecretStr(fallback))

        return self

    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="OpenAI embedding model",
    )
    embedding_dimensions: int = Field(default=1536, description="Embedding vector dimensions")

    # Conventions repository configuration
    conventions_repo_path: Path = Field(
        default=Path(__file__).parent.parent.parent.parent,
        description="Path to conventions repository root",
    )

    # Content paths (relative to conventions_repo_path)
    wisdom_path: str = Field(
        default="docs/wisdom",
        description="Path to wisdom documentation",
    )
    templates_path: str = Field(
        default="templates",
        description="Path to templates directory",
    )
    configs_path: str = Field(
        default="configs",
        description="Path to config templates directory",
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
    def falkordb_url(self) -> str:
        """Construct FalkorDB connection URL."""
        return f"redis://:{self.falkordb_password}@{self.falkordb_host}:{self.falkordb_port}"

    @property
    def postgres_url(self) -> str:
        """Construct PostgreSQL connection URL for asyncpg."""
        password = self.postgres_password.get_secret_value()
        return f"postgresql+asyncpg://{self.postgres_user}:{password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    @property
    def postgres_url_sync(self) -> str:
        """Construct PostgreSQL connection URL for sync operations (Alembic)."""
        password = self.postgres_password.get_secret_value()
        return f"postgresql://{self.postgres_user}:{password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"


# Global settings instance
settings = Settings()
