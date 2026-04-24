"""Configuration management for Sibyl MCP Server."""

import os
import secrets
from pathlib import Path
from typing import Literal

import structlog
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sibyl.runtime_shape import (
    default_auth_store,
    fully_surreal_runtime,
    requires_relational_support,
    resolve_coordination_backend,
    uses_relational_auth,
)

_log = structlog.get_logger()

# Persisted auto-generated JWT key (same pattern as settings.key in crypto.py)
_JWT_KEY_FILE = Path.home() / ".sibyl" / "jwt.key"


def _get_or_create_jwt_secret() -> str:
    """Read persisted JWT secret from ~/.sibyl/jwt.key, or generate and save one."""
    if _JWT_KEY_FILE.exists():
        try:
            key = _JWT_KEY_FILE.read_text().strip()
            if key:
                return key
        except Exception as e:
            _log.warning("Failed to read JWT key file", error=str(e))

    key = secrets.token_hex(32)
    _log.info("Auto-generated JWT secret for development", path=str(_JWT_KEY_FILE))
    try:
        _JWT_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _JWT_KEY_FILE.write_text(key)
        _JWT_KEY_FILE.chmod(0o600)
    except Exception as e:
        _log.warning("Failed to persist JWT key", error=str(e))

    return key


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="SIBYL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server configuration
    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Runtime environment (development, staging, production)",
    )
    server_name: str = Field(default="sibyl", description="MCP server name")
    server_host: str = Field(default="localhost", description="Server bind host")
    server_port: int = Field(default=3334, description="Server bind port")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Logging level",
    )
    store: Literal["legacy", "surreal"] = Field(
        default="legacy",
        description="Active persistence runtime for this process",
    )
    auth_store: Literal["postgres", "surreal"] = Field(
        default="postgres",
        description="Active auth persistence runtime for this process",
    )
    coordination_backend: Literal["auto", "local", "redis"] = Field(
        default="auto",
        description="Coordination backend for jobs, locks, pub/sub, and pending state",
    )

    # Auth configuration
    disable_auth: bool = Field(
        default=False,
        description="Disable auth enforcement (dev mode only)",
    )

    @model_validator(mode="after")
    def validate_security_settings(self) -> "Settings":
        """Prevent insecure settings in production."""
        if self.store == "surreal" and "auth_store" not in self.model_fields_set:
            object.__setattr__(self, "auth_store", default_auth_store(store=self.store))
        if self.environment == "production":
            if self.disable_auth:
                raise ValueError(
                    "CRITICAL: disable_auth=True is forbidden in production environment. "
                    "Set SIBYL_ENVIRONMENT=development to use disable_auth for testing."
                )
            if self.falkordb_password == "conventions":  # noqa: S105
                raise ValueError(
                    "CRITICAL: Default FalkorDB password 'conventions' is forbidden in production. "
                    "Set SIBYL_FALKORDB_PASSWORD to a secure value."
                )
            if self.postgres_password.get_secret_value() == "sibyl_dev":
                raise ValueError(
                    "CRITICAL: Default PostgreSQL password 'sibyl_dev' is forbidden in production. "
                    "Set SIBYL_POSTGRES_PASSWORD to a secure value."
                )
            if self.store == "surreal" and self.resolved_surreal_url.startswith("memory://"):
                raise ValueError(
                    "CRITICAL: In-memory SurrealDB is forbidden in production. "
                    "Set SIBYL_SURREAL_URL or SIBYL_SURREAL_DATA_DIR."
                )
        return self

    jwt_secret: SecretStr = Field(
        default=SecretStr(""),
        description="JWT signing secret (required for auth)",
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT signing algorithm")
    access_token_expire_minutes: int = Field(
        default=60, ge=5, le=1440, description="Access token TTL (minutes, default 1 hour)"
    )
    refresh_token_expire_days: int = Field(
        default=30, ge=1, le=365, description="Refresh token TTL (days, default 30 days)"
    )

    github_client_id: SecretStr = Field(default=SecretStr(""), description="GitHub OAuth client id")
    github_client_secret: SecretStr = Field(
        default=SecretStr(""), description="GitHub OAuth client secret"
    )

    # Public URL - single source of truth for all external URLs
    # When using Kong/ingress, both API and frontend are on the same domain
    public_url: str = Field(
        default="http://localhost:3337",
        description="Public base URL for the application (used for OAuth callbacks, redirects)",
    )

    # These are derived from public_url by default but can be overridden if needed
    server_url: str = Field(
        default="",
        description="Override API base URL (defaults to public_url)",
    )
    frontend_url: str = Field(
        default="",
        description="Override frontend base URL (defaults to public_url)",
    )

    @model_validator(mode="after")
    def derive_urls_from_public(self) -> "Settings":
        """Derive server_url and frontend_url from public_url if not explicitly set."""
        if not self.server_url:
            if "public_url" in self.model_fields_set:
                object.__setattr__(self, "server_url", self.public_url.rstrip("/"))
            else:
                host = self.server_host
                if host in {"0.0.0.0", "::"}:
                    host = "localhost"
                object.__setattr__(self, "server_url", f"http://{host}:{self.server_port}")
        if not self.frontend_url:
            object.__setattr__(self, "frontend_url", self.public_url.rstrip("/") + "/")
        return self

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

    # Rate limiting configuration
    rate_limit_enabled: bool = Field(
        default=True,
        description="Enable rate limiting on API endpoints",
    )
    rate_limit_default: str = Field(
        default="100/minute",
        description="Default rate limit for API endpoints (e.g., '100/minute', '1000/hour')",
    )
    rate_limit_storage: str = Field(
        default="memory://",
        description="Rate limit storage backend (memory://, redis://host:port)",
    )

    # Email configuration (Resend)
    resend_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Resend API key for transactional emails",
    )
    email_from: str = Field(
        default="Sibyl <noreply@sibyl.dev>",
        description="Default from address for emails",
    )

    # FalkorDB configuration
    falkordb_host: str = Field(default="localhost", description="FalkorDB host")
    falkordb_port: int = Field(default=6380, description="FalkorDB port")
    falkordb_password: str = Field(default="conventions", description="FalkorDB password")
    redis_jobs_db: int = Field(
        default=1,
        description="Redis database number for job queue",
    )
    redis_host: str | None = Field(
        default=None,
        description="Redis/Valkey host for jobs, locks, and pub/sub",
    )
    redis_port: int | None = Field(default=None, description="Redis/Valkey port")
    redis_password: SecretStr | None = Field(
        default=None,
        description="Redis/Valkey password",
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
    surreal_namespace_prefix: str = Field(
        default="org_",
        description="Namespace prefix for org-scoped SurrealDB data",
    )
    surreal_database: str = Field(
        default="graph",
        description="SurrealDB database name used inside each org namespace",
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
        if self.store == "surreal" and "auth_store" not in self.model_fields_set:
            object.__setattr__(self, "auth_store", "surreal")

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

        # JWT: fall back to non-prefixed env vars, auto-generate in dev
        if not self.jwt_secret.get_secret_value():
            fallback = os.environ.get("JWT_SECRET", "")
            if fallback:
                object.__setattr__(self, "jwt_secret", SecretStr(fallback))
            elif self.environment != "production":
                object.__setattr__(self, "jwt_secret", SecretStr(_get_or_create_jwt_secret()))

        if self.surreal_url and self.surreal_data_dir:
            raise ValueError("Configure only one of surreal_url or surreal_data_dir")

        if self.redis_host is None:
            object.__setattr__(self, "redis_host", self.falkordb_host)
        if self.redis_port is None:
            object.__setattr__(self, "redis_port", self.falkordb_port)
        if self.redis_password is None:
            object.__setattr__(self, "redis_password", SecretStr(self.falkordb_password))

        return self

    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="OpenAI embedding model",
    )
    embedding_dimensions: int = Field(default=1536, description="Embedding vector dimensions")
    graph_embedding_dimensions: int = Field(
        default=1024,
        description="Graph (Graphiti) embedding dimensions; sets EMBEDDING_DIM for vector search",
    )
    graphiti_semaphore_limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Graphiti concurrent LLM operations limit (controls SEMAPHORE_LIMIT)",
    )

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

    # Backup configuration
    backup_dir: Path = Field(
        default=Path("./backups"),
        description="Directory to store backup archives",
    )
    backup_retention_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="Number of days to retain backups before auto-cleanup",
    )
    backup_schedule: str = Field(
        default="0 2 * * *",
        description="Cron schedule for automatic backups (default: 2 AM daily)",
    )
    backup_enabled: bool = Field(
        default=True,
        description="Enable scheduled automatic backups",
    )

    @property
    def falkordb_url(self) -> str:
        """Construct FalkorDB connection URL."""
        return f"redis://:{self.falkordb_password}@{self.falkordb_host}:{self.falkordb_port}"

    @property
    def redis_password_value(self) -> str:
        """Resolve the active Redis password with FalkorDB fallback."""
        if self.redis_password is None:
            return self.falkordb_password
        return self.redis_password.get_secret_value()

    @property
    def redis_url(self) -> str:
        """Construct Redis/Valkey connection URL."""
        auth = f":{self.redis_password_value}@" if self.redis_password_value else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}"

    @property
    def resolved_surreal_url(self) -> str:
        """Construct the effective SurrealDB connection URL."""
        if self.surreal_url:
            return self.surreal_url
        if self.surreal_data_dir:
            return f"surrealkv://{self.surreal_data_dir}"
        return "memory://"

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

    @property
    def fully_surreal(self) -> bool:
        """Whether both the main store and auth runtime are fully Surreal-backed."""
        return fully_surreal_runtime(store=self.store, auth_store=self.auth_store)

    @property
    def uses_relational_auth(self) -> bool:
        """Whether auth/session persistence still depends on PostgreSQL."""
        return uses_relational_auth(auth_store=self.auth_store)

    @property
    def requires_relational_support(self) -> bool:
        """Whether startup/runtime helpers still need relational services online."""
        return requires_relational_support(store=self.store, auth_store=self.auth_store)

    @property
    def resolved_coordination_backend(self) -> Literal["local", "redis"]:
        """Resolve the active coordination backend for this runtime."""
        return resolve_coordination_backend(
            store=self.store,
            coordination_backend=self.coordination_backend,
        )


# Global settings instance
settings = Settings()
