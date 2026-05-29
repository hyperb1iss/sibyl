"""Configuration management for Sibyl MCP Server."""

import os
import secrets
from datetime import UTC, datetime
from ipaddress import ip_network
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit, urlunsplit

import structlog
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sibyl.runtime_shape import (
    default_auth_store,
    fully_surreal_runtime,
    resolve_coordination_backend,
    uses_relational_auth,
)

_log = structlog.get_logger()

# Persisted auto-generated JWT key (same pattern as settings.key in crypto.py)
_JWT_KEY_FILE = Path.home() / ".sibyl" / "jwt.key"
_EXTRA_OIDC_PROVIDER_NAMES = {"github", "google"}
_EXTRA_OIDC_ISSUER_HOSTS = {"github.com", "accounts.google.com"}


class OIDCProviderSettings(BaseModel):
    name: str
    issuer: str
    client_id: str
    client_secret_env: str
    scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email"])
    role_claim_override: str | None = None

    @field_validator("name", "issuer", "client_id", "client_secret_env")
    @classmethod
    def strip_required_string(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("OIDC provider fields must not be empty")
        return stripped

    @field_validator("role_claim_override")
    @classmethod
    def strip_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("scopes")
    @classmethod
    def normalize_scopes(cls, value: list[str]) -> list[str]:
        scopes = [scope.strip() for scope in value if scope.strip()]
        if "openid" not in scopes:
            raise ValueError("OIDC provider scopes must include openid")
        return scopes

    @property
    def is_extra_provider(self) -> bool:
        normalized_name = self.name.strip().lower().replace("_", "-")
        issuer_host = urlsplit(self.issuer).netloc.lower()
        return (
            normalized_name in _EXTRA_OIDC_PROVIDER_NAMES or issuer_host in _EXTRA_OIDC_ISSUER_HOSTS
        )


class OIDCSettings(BaseModel):
    providers: list[OIDCProviderSettings] = Field(default_factory=list)
    role_claim: str = "roles"
    redirect_uri_base: str = ""
    session_minutes: int = Field(default=60, ge=5, le=1440)
    silent_refresh_enabled: bool = False
    extra_providers_enabled: bool = False

    @field_validator("role_claim", "redirect_uri_base")
    @classmethod
    def strip_string(cls, value: str) -> str:
        return value.strip()


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


def _redis_url_with_password(url: str, password: str) -> str:
    """Inject the Redis password into a redis:// URL when it omits auth."""
    parsed = urlsplit(url)
    if parsed.scheme != "redis" or "@" in parsed.netloc or not password:
        return url

    return urlunsplit(
        (
            parsed.scheme,
            f":{quote(password, safe='')}@{parsed.netloc}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


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
    store: Literal["surreal"] = Field(
        default="surreal",
        description="Active persistence runtime for this process",
    )
    auth_store: Literal["surreal"] = Field(
        default="surreal",
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
    public_signups_enabled: bool = Field(
        default=False,
        description="Allow self-serve local account creation after initial setup",
    )
    local_auth_enabled: bool = Field(
        default=False,
        description="Enable local username/password login outside setup and break-glass flows",
    )
    break_glass_enabled: bool = Field(
        default=False,
        description="Treat local auth as an active break-glass access path",
    )
    break_glass_allowed_ips: list[str] = Field(
        default_factory=list,
        description="Optional CIDR ranges allowed to use break-glass local auth",
    )
    break_glass_expires_at: datetime | None = Field(
        default=None,
        description="UTC timestamp after which break-glass local auth is denied",
    )
    oidc: OIDCSettings = Field(
        default_factory=OIDCSettings,
        description="OpenID Connect provider and session settings",
    )

    @field_validator("break_glass_allowed_ips")
    @classmethod
    def validate_break_glass_allowed_ips(cls, value: list[str]) -> list[str]:
        cidrs = [cidr.strip() for cidr in value if cidr.strip()]
        for cidr in cidrs:
            ip_network(cidr, strict=False)
        return cidrs

    @field_validator("break_glass_expires_at")
    @classmethod
    def normalize_break_glass_expires_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_security_settings(self) -> "Settings":
        """Prevent insecure settings in production."""
        if "auth_store" not in self.model_fields_set:
            object.__setattr__(self, "auth_store", default_auth_store(store=self.store))
        if "local_auth_enabled" not in self.model_fields_set and self.environment == "development":
            object.__setattr__(self, "local_auth_enabled", True)
        if self.environment == "production":
            if self.disable_auth:
                raise ValueError(
                    "CRITICAL: disable_auth=True is forbidden in production environment. "
                    "Set SIBYL_ENVIRONMENT=development to use disable_auth for testing."
                )
            if self.auth_store == "surreal" and self.resolved_surreal_url.startswith("memory://"):
                raise ValueError(
                    "CRITICAL: In-memory SurrealDB is forbidden in production. "
                    "Set SIBYL_SURREAL_URL or SIBYL_SURREAL_DATA_DIR."
                )
            if (
                self.auth_store == "surreal"
                and self.resolved_surreal_url.startswith("surrealkv://")
                and not self.allow_embedded_single_writer
            ):
                raise ValueError(
                    "CRITICAL: Embedded SurrealDB requires explicit single-writer opt-in in "
                    "production. Set SIBYL_ALLOW_EMBEDDED_SINGLE_WRITER=1 only when one "
                    "daemon owns the database."
                )
            if self.cookie_secure is False:
                raise ValueError(
                    "CRITICAL: cookie_secure=False is forbidden in production. "
                    "Use Secure cookies for production auth sessions."
                )
            if self.auth_store == "surreal":
                insecure_pairs = {
                    ("root", "sibyl_dev"),
                    ("root", "root"),
                    ("change-me", "change-me-strong-password"),
                }
                if (
                    self.surreal_username,
                    self.surreal_password.get_secret_value(),
                ) in insecure_pairs:
                    raise ValueError(
                        "CRITICAL: Default SurrealDB credentials are forbidden in production. "
                        "Set SIBYL_SURREAL_USERNAME and SIBYL_SURREAL_PASSWORD to secure values."
                    )
        return self

    @model_validator(mode="after")
    def validate_oidc_settings(self) -> "Settings":
        if not self.oidc.extra_providers_enabled:
            blocked = [
                provider.name for provider in self.oidc.providers if provider.is_extra_provider
            ]
            if blocked:
                joined = ", ".join(sorted(blocked))
                raise ValueError(
                    f"OIDC extra providers require oidc.extra_providers_enabled=true: {joined}"
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
    allow_embedded_single_writer: bool = Field(
        default=False,
        description="Allow embedded SurrealDB storage in production for explicit single-writer mode",
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

    metrics_scrape_token: SecretStr = Field(
        default=SecretStr(""),
        description="Bearer token required for the root /metrics scrape endpoint outside local dev",
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
    email_outbox_path: str = Field(
        default="",
        description="Optional JSONL outbox path for local/staging email capture",
    )

    redis_jobs_db: int = Field(
        default=1,
        description="Redis database number for job queue",
    )
    redis_host: str = Field(
        default="127.0.0.1",
        description="Redis/Valkey host for jobs, locks, and pub/sub",
    )
    redis_port: int = Field(default=6381, description="Redis/Valkey port")
    redis_password: SecretStr = Field(
        default=SecretStr(""),
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

    # Gemini configuration (SIBYL_GEMINI_API_KEY, GEMINI_API_KEY, or GOOGLE_API_KEY)
    gemini_api_key: SecretStr = Field(
        default=SecretStr(""), description="Gemini API key for Google embeddings"
    )

    @model_validator(mode="after")
    def check_api_key_fallbacks(self) -> "Settings":
        """Fall back to non-prefixed env vars for API keys."""
        if "auth_store" not in self.model_fields_set:
            object.__setattr__(self, "auth_store", default_auth_store(store=self.store))
        if "local_auth_enabled" not in self.model_fields_set and self.environment == "development":
            object.__setattr__(self, "local_auth_enabled", True)

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

        # Gemini: check GEMINI_API_KEY then GOOGLE_API_KEY if SIBYL_GEMINI_API_KEY not set
        if not self.gemini_api_key.get_secret_value():
            fallback = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
            if fallback:
                object.__setattr__(self, "gemini_api_key", SecretStr(fallback))

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

        if self.rate_limit_storage.startswith("redis://"):
            storage_url = _redis_url_with_password(
                self.rate_limit_storage,
                self.redis_password_value,
            )
            object.__setattr__(self, "rate_limit_storage", storage_url)

        return self

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
    auto_extract_entities: bool = Field(
        default=False,
        description="Queue LLM-powered entity extraction for prose-bearing memories",
    )
    memory_extraction_max_queue_depth: int = Field(
        default=250,
        ge=1,
        description="Maximum pending job depth before memory extraction enqueue backpressure",
    )
    memory_extraction_max_sources_per_job: int = Field(
        default=24,
        ge=1,
        le=100,
        description="Maximum memory sources in one LLM extraction job",
    )
    memory_extraction_max_source_chars: int = Field(
        default=12_000,
        ge=500,
        le=50_000,
        description="Maximum characters sent to extraction from a single memory source",
    )
    memory_extraction_max_job_chars: int = Field(
        default=192_000,
        ge=1_000,
        le=500_000,
        description="Maximum source characters in one memory extraction job",
    )
    memory_extraction_max_entities_per_source: int = Field(
        default=4,
        ge=1,
        le=12,
        description="Maximum extracted graph entities requested per memory source",
    )
    memory_extraction_max_concurrency: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Maximum concurrent LLM calls inside one memory extraction job",
    )
    memory_extraction_max_tokens: int = Field(
        default=8192,
        ge=256,
        le=8192,
        description="Maximum output tokens for one memory extraction LLM call",
    )

    # Knowledge repository configuration
    knowledge_repo_path: Path = Field(
        default=Path(__file__).parent.parent.parent.parent,
        description="Path to knowledge repository root",
    )

    # Content paths (relative to knowledge_repo_path)
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
    source_import_dir: Path = Field(
        default=Path("./source-imports"),
        description="Directory containing local source archives that API imports may read",
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
    def redis_password_value(self) -> str:
        """Resolve the active Redis password."""
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
    def fully_surreal(self) -> bool:
        """Whether both the main store and auth runtime are fully Surreal-backed."""
        return fully_surreal_runtime(store=self.store, auth_store=self.auth_store)

    @property
    def uses_relational_auth(self) -> bool:
        """Whether auth/session persistence still depends on PostgreSQL."""
        return uses_relational_auth(auth_store=self.auth_store)

    @property
    def resolved_coordination_backend(self) -> Literal["local", "redis"]:
        """Resolve the active coordination backend for this runtime."""
        return resolve_coordination_backend(
            store=self.store,
            coordination_backend=self.coordination_backend,
        )


# Global settings instance
settings = Settings()


def reload_settings_from_env() -> Settings:
    """Reload the global settings instance from current environment variables."""
    refreshed = Settings()
    settings.__dict__.update(refreshed.__dict__)
    return settings
