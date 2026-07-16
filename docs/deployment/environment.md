# Environment Variables Reference

Complete reference for all Sibyl environment variables.

## Configuration Loading

Sibyl uses Pydantic Settings to load configuration from the process environment:

1. Environment variables (highest priority)
2. Explicit deployment env files loaded by the launcher (`docker compose --env-file`, systemd
   `EnvironmentFile`, Kubernetes secrets, etc.)
3. Default values

Local development does not read repo `.env` files. Use shell exports, the web onboarding UI, or an
explicit deployment env file.

All variables use the `SIBYL_` prefix. Some common variables (API keys) also support unprefixed
versions as fallbacks.

## Server Configuration

| Variable            | Default       | Description                                         |
| ------------------- | ------------- | --------------------------------------------------- |
| `SIBYL_ENVIRONMENT` | `development` | Runtime environment: development/staging/production |
| `SIBYL_SERVER_NAME` | `sibyl`       | MCP server name                                     |
| `SIBYL_SERVER_HOST` | `localhost`   | Server bind host                                    |
| `SIBYL_SERVER_PORT` | `3334`        | Server bind port                                    |
| `SIBYL_LOG_LEVEL`   | `INFO`        | Logging level: DEBUG/INFO/WARNING/ERROR             |

## Storage Mode

| Variable                     | Default   | Description                                       |
| ---------------------------- | --------- | ------------------------------------------------- |
| `SIBYL_STORE`                | `surreal` | Active persistence runtime                        |
| `SIBYL_AUTH_STORE`           | `surreal` | Auth persistence. Only `surreal` is supported     |
| `SIBYL_COORDINATION_BACKEND` | `auto`    | Jobs, locks, pub/sub: `auto`, `local`, or `redis` |

`auto` resolves to local in-process coordination for the default Surreal runtime. Use `redis` for
multi-pod deployments. See [storage-modes.md](../guide/storage-modes.md) for the full mode matrix.

## SurrealDB

SurrealDB is the default and only runtime store. These settings apply to every Sibyl process.

| Variable                         | Default | Description                                                           |
| -------------------------------- | ------- | --------------------------------------------------------------------- |
| `SIBYL_SURREAL_URL`              | (empty) | Connection URL (`ws://`, `http://`, `surrealkv://`, `memory://`)      |
| `SIBYL_SURREAL_DATA_DIR`         | (empty) | Local SurrealKV path used when `SIBYL_SURREAL_URL` is unset           |
| `SIBYL_SURREAL_USERNAME`         | (empty) | Root username for remote runtimes                                     |
| `SIBYL_SURREAL_PASSWORD`         | (empty) | Root password for remote runtimes                                     |
| `SIBYL_SURREAL_TOKEN`            | (empty) | Bearer token for remote runtimes (alternative to username/password)   |
| `SIBYL_SURREAL_NAMESPACE_PREFIX` | `org_`  | Namespace prefix for per-org isolation (`org_<uuid_hex>`)             |
| `SIBYL_SURREAL_DATABASE`         | `graph` | Database name inside each org namespace                               |
| `SIBYL_SURREAL_SLOW_QUERY_MS`    | `500`   | Log SurrealDB queries at warning level when elapsed time exceeds this |

`SIBYL_SURREAL_URL` and `SIBYL_SURREAL_DATA_DIR` are mutually exclusive; set only one. When neither
is set, Sibyl falls back to in-memory mode. In-memory mode (`memory://`) is rejected in production.

## URL Configuration

| Variable             | Default                   | Description                                    |
| -------------------- | ------------------------- | ---------------------------------------------- |
| `SIBYL_PUBLIC_URL`   | `http://localhost:3337`   | Public base URL for OAuth callbacks, redirects |
| `SIBYL_SERVER_URL`   | (derived from public_url) | API base URL override                          |
| `SIBYL_FRONTEND_URL` | (derived from public_url) | Frontend base URL override                     |

When using Kong or similar ingress, `SIBYL_PUBLIC_URL` is typically set to the external domain
(e.g., `https://sibyl.example.com`), and both API and frontend are served from the same origin.

## Authentication

| Variable                            | Default    | Description                                             |
| ----------------------------------- | ---------- | ------------------------------------------------------- |
| `SIBYL_JWT_SECRET`                  | (dev auto) | JWT signing secret, required in production              |
| `SIBYL_JWT_ALGORITHM`               | `HS256`    | JWT signing algorithm                                   |
| `SIBYL_ACCESS_TOKEN_EXPIRE_MINUTES` | `60`       | Access token TTL in minutes                             |
| `SIBYL_REFRESH_TOKEN_EXPIRE_DAYS`   | `30`       | Local-auth refresh token TTL in days                    |
| `SIBYL_DISABLE_AUTH`                | `false`    | Disable auth enforcement (dev only)                     |
| `SIBYL_MCP_AUTH_MODE`               | `auto`     | MCP auth: auto/on/off                                   |
| `SIBYL_SETTINGS_KEY`                | (auto)     | Fernet key for encrypting DB-stored secrets             |
| `SIBYL_LOCAL_AUTH_ENABLED`          | `true`     | Enable local username/password login after setup        |
| `SIBYL_PUBLIC_SIGNUPS_ENABLED`      | `false`    | Allow public self-serve account creation after setup    |
| `SIBYL_OIDC`                        | `{}`       | JSON object for optional OIDC providers and session UX  |
| `SIBYL_BREAK_GLASS_ENABLED`         | `false`    | Enable bounded emergency local login for SSO outages    |
| `SIBYL_BREAK_GLASS_ALLOWED_IPS`     | `[]`       | JSON array of CIDRs allowed to use break-glass login    |
| `SIBYL_BREAK_GLASS_EXPIRES_AT`      | (empty)    | UTC expiry for break-glass, no more than four hours out |

The default Sibyl mode is local-first and single-user friendly: local auth is enabled, the first
setup signup creates the owner/admin user, and account creation after setup is invite-based unless
`SIBYL_PUBLIC_SIGNUPS_ENABLED=true`.

OIDC, silent refresh, extra OAuth providers, public signups, disabled local auth, and break-glass
are all opt-in. Enterprise SSO deployments should configure a corporate OIDC provider first, verify
an owner can sign in through it, and only then set `SIBYL_LOCAL_AUTH_ENABLED=false`.

`SIBYL_OIDC` is a JSON object with these fields:

```json
{
  "providers": [
    {
      "name": "entra",
      "issuer": "https://login.microsoftonline.com/<tenant-id>/v2.0",
      "client_id": "<app-client-id>",
      "client_secret_env": "SIBYL_OIDC_ENTRA_CLIENT_SECRET",
      "organization_slug": "acme",
      "scopes": ["openid", "profile", "email"]
    }
  ],
  "role_claim": "roles",
  "redirect_uri_base": "",
  "session_minutes": 60,
  "silent_refresh_enabled": false,
  "extra_providers_enabled": false
}
```

Every provider requires `organization_slug`. It binds that provider to one exact non-personal
organization; OIDC login never chooses an organization from the user's other memberships.

Non-corporate providers such as GitHub or Google require `"extra_providers_enabled": true`; leave it
false for enterprise SSO.

### Fallback Variables

These unprefixed variables are checked if `SIBYL_*` versions are empty:

- `JWT_SECRET` -> `SIBYL_JWT_SECRET`

### Security Warning

```bash
# NEVER set disable_auth in production!
# This validation is enforced:
if environment == "production" and disable_auth:
    raise ValueError("disable_auth=True is forbidden in production")
```

## GitHub OAuth

| Variable                     | Default | Description                     |
| ---------------------------- | ------- | ------------------------------- |
| `SIBYL_GITHUB_CLIENT_ID`     | (empty) | GitHub OAuth application ID     |
| `SIBYL_GITHUB_CLIENT_SECRET` | (empty) | GitHub OAuth application secret |

Fallbacks:

- `GITHUB_CLIENT_ID` -> `SIBYL_GITHUB_CLIENT_ID`
- `GITHUB_CLIENT_SECRET` -> `SIBYL_GITHUB_CLIENT_SECRET`

## Cookie Configuration

| Variable              | Default | Description                                  |
| --------------------- | ------- | -------------------------------------------- |
| `SIBYL_COOKIE_DOMAIN` | (none)  | Cookie domain override                       |
| `SIBYL_COOKIE_SECURE` | (auto)  | Force Secure cookies (auto-detects from URL) |

## Password Hashing

| Variable                    | Default  | Description                          |
| --------------------------- | -------- | ------------------------------------ |
| `SIBYL_PASSWORD_PEPPER`     | (empty)  | Optional pepper for password hashing |
| `SIBYL_PASSWORD_ITERATIONS` | `310000` | PBKDF2-HMAC-SHA256 iterations        |

## Rate Limiting

| Variable                   | Default      | Description                             |
| -------------------------- | ------------ | --------------------------------------- |
| `SIBYL_RATE_LIMIT_ENABLED` | `true`       | Enable rate limiting                    |
| `SIBYL_RATE_LIMIT_DEFAULT` | `100/minute` | Default rate limit                      |
| `SIBYL_RATE_LIMIT_STORAGE` | `memory://`  | Storage backend (memory:// or redis://) |

## PostgreSQL (migration/rehearsal only)

PostgreSQL is not part of the Sibyl runtime and is not read by the `Settings` model. It is consumed
only by the standalone `migrate` CLI when explicitly restoring a retained `postgres.sql` payload
against an operator-managed PostgreSQL database. Structured auth and content archive export reads
SurrealDB. Remove any stale `SIBYL_AUTH_STORE=postgres` value before starting the API.

The variables below are **not** `SIBYL_` Settings fields. They are read directly by the migration
tooling for a rehearsal database connection and are ignored by the running API and worker.

| Variable                      | Default     | Description                                 |
| ----------------------------- | ----------- | ------------------------------------------- |
| `SIBYL_POSTGRES_HOST`         | `localhost` | External rehearsal database host            |
| `SIBYL_POSTGRES_PORT`         | `5433`      | External rehearsal database port            |
| `SIBYL_POSTGRES_USER`         | `sibyl`     | External rehearsal database username        |
| `SIBYL_POSTGRES_PASSWORD`     | `sibyl_dev` | External rehearsal database password        |
| `SIBYL_POSTGRES_DB`           | `sibyl`     | External rehearsal database name            |
| `SIBYL_POSTGRES_POOL_SIZE`    | `10`        | External rehearsal database connection pool |
| `SIBYL_POSTGRES_MAX_OVERFLOW` | `20`        | External rehearsal database overflow limit  |

Configure them only when running a migration that explicitly restores a retained `postgres.sql`
payload. They have no effect on the default Surreal runtime.

## Redis/Valkey Coordination

Redis/Valkey is optional. The default Surreal runtime uses local in-process coordination.

| Variable               | Default     | Description            |
| ---------------------- | ----------- | ---------------------- |
| `SIBYL_REDIS_HOST`     | `127.0.0.1` | Redis/Valkey host      |
| `SIBYL_REDIS_PORT`     | `6381`      | Redis/Valkey port      |
| `SIBYL_REDIS_PASSWORD` | -           | Redis/Valkey password  |
| `SIBYL_REDIS_JOBS_DB`  | `1`         | Redis DB for job queue |

## LLM Configuration

| Variable             | Default            | Description                       |
| -------------------- | ------------------ | --------------------------------- |
| `SIBYL_LLM_PROVIDER` | `anthropic`        | LLM provider: openai or anthropic |
| `SIBYL_LLM_MODEL`    | `claude-haiku-4-5` | LLM model for entity extraction   |

## Embeddings

Document chunk embeddings and graph node/relationship embeddings are configured separately. The
graph embedding dimensions also size the native Surreal vector indexes.

| Variable                           | Default                  | Description                                         |
| ---------------------------------- | ------------------------ | --------------------------------------------------- |
| `SIBYL_EMBEDDING_PROVIDER`         | `openai`                 | Document chunk embedding provider: openai or gemini |
| `SIBYL_EMBEDDING_MODEL`            | `text-embedding-3-small` | Document chunk embedding model                      |
| `SIBYL_EMBEDDING_DIMENSIONS`       | `1536`                   | Document chunk embedding vector dimensions          |
| `SIBYL_GRAPH_EMBEDDING_PROVIDER`   | `openai`                 | Graph node/relationship embedding provider          |
| `SIBYL_GRAPH_EMBEDDING_MODEL`      | `text-embedding-3-small` | Graph node/relationship embedding model             |
| `SIBYL_GRAPH_EMBEDDING_DIMENSIONS` | `1024`                   | Graph embedding dimensions (sizes vector indexes)   |

## API Keys

| Variable                  | Default | Description                              |
| ------------------------- | ------- | ---------------------------------------- |
| `SIBYL_OPENAI_API_KEY`    | (empty) | OpenAI API key (required for embeddings) |
| `SIBYL_ANTHROPIC_API_KEY` | (empty) | Anthropic API key                        |
| `SIBYL_GEMINI_API_KEY`    | (empty) | Gemini API key (for Google embeddings)   |

### Lookup Priority

API keys are resolved in this order:

1. **Database** - Keys stored via web UI (Settings, AI Services)
2. **Environment variables** - `SIBYL_OPENAI_API_KEY`, `SIBYL_ANTHROPIC_API_KEY`,
   `SIBYL_GEMINI_API_KEY`
3. **Unprefixed fallbacks** - `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` /
   `GOOGLE_API_KEY`

This allows zero-config deployments where API keys are entered through the onboarding wizard and
stored encrypted in the database (using `SIBYL_SETTINGS_KEY`).

### Unprefixed Fallbacks

- `OPENAI_API_KEY` -> `SIBYL_OPENAI_API_KEY`
- `ANTHROPIC_API_KEY` -> `SIBYL_ANTHROPIC_API_KEY`
- `GEMINI_API_KEY` or `GOOGLE_API_KEY` -> `SIBYL_GEMINI_API_KEY`

## Native Memory Configuration

| Variable             | Default   | Description                                             |
| -------------------- | --------- | ------------------------------------------------------- |
| `SIBYL_NATIVE_WRITE` | `enabled` | Set `disabled` to skip persisting reflection candidates |

## Runtime Telemetry

| Variable                     | Default | Description                                           |
| ---------------------------- | ------- | ----------------------------------------------------- |
| `SIBYL_METRICS_SCRAPE_TOKEN` | (empty) | Bearer/header token for non-local `/metrics` scraping |

## Email

Sibyl sends through SMTP when `SIBYL_SMTP_HOST` is set, otherwise through Resend when
`SIBYL_RESEND_API_KEY` is set. The JSONL outbox writes regardless of live delivery provider. When
setting SMTP passwords in a Compose `.env` file, escape literal `$` characters as `$$`.

| Variable                     | Default                     | Description                                          |
| ---------------------------- | --------------------------- | ---------------------------------------------------- |
| `SIBYL_RESEND_API_KEY`       | (empty)                     | Resend API key for transactional email               |
| `SIBYL_SMTP_HOST`            | (empty)                     | SMTP host for transactional email                    |
| `SIBYL_SMTP_PORT`            | `587`                       | SMTP port                                            |
| `SIBYL_SMTP_USERNAME`        | (empty)                     | SMTP authentication username                         |
| `SIBYL_SMTP_PASSWORD`        | (empty)                     | SMTP authentication password                         |
| `SIBYL_SMTP_STARTTLS`        | `true`                      | Upgrade SMTP connection with STARTTLS                |
| `SIBYL_SMTP_SSL`             | `false`                     | Use implicit TLS instead of STARTTLS                 |
| `SIBYL_SMTP_TIMEOUT_SECONDS` | `20`                        | SMTP connection timeout                              |
| `SIBYL_EMAIL_FROM`           | `Sibyl <noreply@sibyl.dev>` | Default from address                                 |
| `SIBYL_EMAIL_OUTBOX_PATH`    | (empty)                     | Optional JSONL outbox path for local/staging capture |

## Content Ingestion

| Variable                     | Default            | Description                                             |
| ---------------------------- | ------------------ | ------------------------------------------------------- |
| `SIBYL_CHUNK_MAX_TOKENS`     | `1000`             | Maximum tokens per chunk                                |
| `SIBYL_CHUNK_OVERLAP_TOKENS` | `100`              | Token overlap between chunks                            |
| `SIBYL_SOURCE_IMPORT_DIR`    | `./source-imports` | Directory of local source archives API imports may read |

## Backups

Scheduled archive backups run from the worker. See [Monitoring](monitoring.md) for operational
detail.

| Variable                      | Default     | Description                                      |
| ----------------------------- | ----------- | ------------------------------------------------ |
| `SIBYL_BACKUP_ENABLED`        | `true`      | Enable scheduled automatic backups               |
| `SIBYL_BACKUP_DIR`            | `./backups` | Directory to store backup archives               |
| `SIBYL_BACKUP_RETENTION_DAYS` | `30`        | Days to retain backups before auto-cleanup       |
| `SIBYL_BACKUP_SCHEDULE`       | `0 2 * * *` | Cron schedule for automatic backups (2 AM daily) |

## Worker Configuration

| Variable           | Default | Description                                                         |
| ------------------ | ------- | ------------------------------------------------------------------- |
| `SIBYL_RUN_WORKER` | `false` | Embed a worker in the API process when Redis coordination is active |

## Example Environment Blocks

### Local Development Shell

```bash
export SIBYL_ENVIRONMENT=development

# Recommended local runtime
export SIBYL_STORE=surreal
export SIBYL_COORDINATION_BACKEND=local
export SIBYL_SURREAL_URL=ws://127.0.0.1:8000/rpc
export SIBYL_SURREAL_USERNAME=root
export SIBYL_SURREAL_PASSWORD=root

# LLM
export SIBYL_OPENAI_API_KEY=sk-...
export SIBYL_ANTHROPIC_API_KEY=sk-ant-...

# Logging
export SIBYL_LOG_LEVEL=DEBUG
```

### Production Env File (Surreal, Default)

```bash
SIBYL_ENVIRONMENT=production
SIBYL_JWT_SECRET=<generate with: openssl rand -hex 32>
SIBYL_SETTINGS_KEY=<generate with: openssl rand -base64 32 | tr '+/' '-_'>

# Public URL (Kong/ingress domain)
SIBYL_PUBLIC_URL=https://sibyl.example.com

# Storage (fully Surreal)
SIBYL_STORE=surreal
SIBYL_AUTH_STORE=surreal
SIBYL_SURREAL_URL=ws://prod-surrealdb.internal:8000/rpc
SIBYL_SURREAL_USERNAME=root
SIBYL_SURREAL_PASSWORD=<secure-password>

# LLM
SIBYL_OPENAI_API_KEY=sk-...
SIBYL_ANTHROPIC_API_KEY=sk-ant-...
SIBYL_LLM_PROVIDER=anthropic
SIBYL_LLM_MODEL=claude-sonnet-4

# Email
SIBYL_SMTP_HOST=smtp.gmail.com
SIBYL_SMTP_PORT=587
SIBYL_SMTP_USERNAME=sibyl@example.com
SIBYL_SMTP_PASSWORD=<google-app-password>
SIBYL_SMTP_STARTTLS=true
SIBYL_EMAIL_FROM=Sibyl <sibyl@example.com>
```

### Migration Archive Rehearsal

Use PostgreSQL settings only when explicitly restoring or validating a retained `postgres.sql`
payload. New production deployments should use the fully Surreal example above.

```bash
SIBYL_ENVIRONMENT=production
SIBYL_JWT_SECRET=<generate with: openssl rand -hex 32>
SIBYL_SETTINGS_KEY=<generate with: openssl rand -base64 32 | tr '+/' '-_'>
SIBYL_PUBLIC_URL=https://sibyl.example.com

# Surreal target
SIBYL_SURREAL_URL=ws://prod-surrealdb.internal:8000/rpc
SIBYL_SURREAL_USERNAME=root
SIBYL_SURREAL_PASSWORD=<secure-password>

# Optional historical archive rehearsal database
SIBYL_POSTGRES_HOST=prod-postgres.internal
SIBYL_POSTGRES_PORT=5433
SIBYL_POSTGRES_PASSWORD=<secure-password>

# LLM
SIBYL_OPENAI_API_KEY=sk-...
SIBYL_ANTHROPIC_API_KEY=sk-ant-...

# Rate limiting with Redis
SIBYL_RATE_LIMIT_STORAGE=redis://prod-redis.internal:6379
```

### Kubernetes ConfigMap

Non-secret environment variables in ConfigMap:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: sibyl-config
  namespace: sibyl
data:
  SIBYL_ENVIRONMENT: "production"
  SIBYL_SERVER_HOST: "0.0.0.0"
  SIBYL_SERVER_PORT: "3334"
  SIBYL_PUBLIC_URL: "https://sibyl.example.com"
  SIBYL_LLM_PROVIDER: "anthropic"
  SIBYL_LLM_MODEL: "claude-haiku-4-5"
  SIBYL_EMBEDDING_MODEL: "text-embedding-3-small"
  SIBYL_EMBEDDING_DIMENSIONS: "1536"
```

### Kubernetes Secret

Sensitive values in Secret:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: sibyl-secrets
  namespace: sibyl
type: Opaque
stringData:
  SIBYL_JWT_SECRET: "<jwt-secret>"
  SIBYL_SETTINGS_KEY: "<fernet-key>" # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  SIBYL_OPENAI_API_KEY: "sk-..." # Optional if using DB-stored keys
  SIBYL_ANTHROPIC_API_KEY: "sk-ant-..." # Optional if using DB-stored keys
  SIBYL_SURREAL_PASSWORD: "<surreal-password>" # For surreal mode
  # Migration/archive only:
  # SIBYL_POSTGRES_PASSWORD: "<db-password>"
```

## Running Multiple Instances

You can run multiple Sibyl instances on the same machine (e.g., dev + test environments) by
configuring different ports and container names.

### Port Configuration

> **Note:** `SIBYL_WEB_PORT` is a docker-compose-level variable used only for port mapping in
> `docker-compose.yml`. It is not consumed by Pydantic Settings or the Python application.

| Variable             | Default | Description                    |
| -------------------- | ------- | ------------------------------ |
| `SIBYL_SERVER_PORT`  | `3334`  | API/MCP server port            |
| `SIBYL_WEB_PORT`     | `3337`  | Web frontend port              |
| `SIBYL_SURREAL_PORT` | `8000`  | SurrealDB port (default store) |
| `SIBYL_BACKEND_URL`  | (auto)  | Backend URL for web app        |

### Quick Setup: Test Instance

1. Export offset ports for this shell:

```bash
export COMPOSE_PROJECT_NAME=sibyl-test
export SIBYL_SERVER_PORT=3344
export SIBYL_WEB_PORT=3347
export SIBYL_SURREAL_PORT=8010
export SIBYL_SURREAL_URL=ws://127.0.0.1:8010/rpc
```

2. Start databases with isolated containers and volumes:

```bash
docker compose --env-file /dev/null -p "$COMPOSE_PROJECT_NAME" up -d
```

3. Start API pointing to test databases:

```bash
sibyld serve
```

4. Start web frontend:

```bash
SIBYL_WEB_PORT=3347 SIBYL_BACKEND_URL=http://localhost:3344 pnpm -C apps/web dev
```

### How It Works

- `COMPOSE_PROJECT_NAME` isolates Docker containers and volumes
- Each port variable controls the corresponding service
- `SIBYL_BACKEND_URL` tells the web frontend where to proxy API requests

### Tips

- Use `docker compose --env-file /dev/null -p sibyl-test ps` to see test instance containers
- Local Surreal data directories are namespaced by project when you override `SURREAL_DATA_DIR`
- CLI contexts let you switch between instances: `sibyl config context use test`

## Computed Properties

The Settings class exposes computed connection URLs and runtime-shape helpers:

```python
settings.resolved_surreal_url  # ws://..., surrealkv://..., or memory://
settings.redis_url             # redis:// URL assembled from host/port/password
settings.fully_surreal         # always True; graph, content, and auth are SurrealDB
settings.resolved_coordination_backend  # resolves "auto" to "local" or "redis"
```

Sibyl is fully SurrealDB-backed, so `fully_surreal` always returns `True`. There are no PostgreSQL
connection helpers or relational-shape flags on `Settings` (`postgres_url`, `postgres_url_sync`,
`uses_relational_auth`, and `requires_relational_support` do not exist). PostgreSQL connection
details live with the migration tooling described under
[PostgreSQL (migration/rehearsal only)](#postgresql-migrationrehearsal-only).
