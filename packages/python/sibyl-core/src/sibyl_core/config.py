"""Core configuration for Sibyl - graph, LLM, and embedding settings.

This module contains settings required by sibyl-core operations.
Server-specific settings (HTTP, PostgreSQL, auth middleware) remain in sibyl-server.
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
        env_file=".env",
        env_file_encoding="utf-8",
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

    # FalkorDB configuration
    falkordb_host: str = Field(default="localhost", description="FalkorDB host")
    falkordb_port: int = Field(default=6380, description="FalkorDB port")
    falkordb_password: str = Field(default="conventions", description="FalkorDB password")

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

        return self

    # Embedding configuration
    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="OpenAI embedding model",
    )
    embedding_dimensions: int = Field(
        default=1536,
        description="Embedding vector dimensions",
    )
    graph_embedding_dimensions: int = Field(
        default=1024,
        description="Graph (Graphiti) embedding dimensions; sets EMBEDDING_DIM for vector search",
    )
    graphiti_semaphore_limit: int = Field(
        default=50,
        ge=1,
        le=100,
        description="Graphiti concurrent operations limit (controls SEMAPHORE_LIMIT). Higher = more parallelism but more FalkorDB load.",
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


# Default core config instance
core_config = CoreConfig()

# Alias for backwards compatibility with tools that import 'settings'
settings = core_config
