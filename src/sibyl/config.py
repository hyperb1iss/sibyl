"""Configuration management for Sibyl MCP Server."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
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

    # FalkorDB configuration
    falkordb_host: str = Field(default="localhost", description="FalkorDB host")
    falkordb_port: int = Field(default=6380, description="FalkorDB port")
    falkordb_password: str = Field(default="", description="FalkorDB password")
    falkordb_graph_name: str = Field(
        default="conventions",
        description="Name of the graph in FalkorDB",
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

    # Anthropic configuration
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Anthropic API key (defaults to ANTHROPIC_API_KEY env var)",
    )

    # OpenAI configuration (for embeddings, or LLM if provider=openai)
    openai_api_key: SecretStr = Field(
        default=SecretStr(""), description="OpenAI API key for embeddings"
    )
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
