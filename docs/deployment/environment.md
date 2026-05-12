# Environment Variables Reference

Complete reference for all Sibyl environment variables.

## Configuration Loading

Sibyl uses Pydantic Settings to load configuration:

1. Environment variables (highest priority)
2. `.env` file in `apps/api/`
3. Default values

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

Used when `SIBYL_STORE=surreal` or for the always-on Surreal auth runtime.

| Variable                         | Default | Description                                                      |
| -------------------------------- | ------- | ---------------------------------------------------------------- |
| `SIBYL_SURREAL_URL`              | (empty) | Connection URL (`ws://`, `http://`, `surrealkv://`, `memory://`) |
| `SIBYL_SURREAL_DATA_DIR`         | (empty) | Local RocksDB path used when `SIBYL_SURREAL_URL` is unset        |
| `SIBYL_SURREAL_USERNAME`         | (empty) | Root username for remote runtimes                                |
| `SIBYL_SURREAL_PASSWORD`         | (empty) | Root password for remote runtimes                                |
| `SIBYL_SURREAL_NAMESPACE_PREFIX` | `org_`  | Namespace prefix for per-org isolation (`org_<uuid_hex>`)        |
| `SIBYL_SURREAL_DATABASE`         | `graph` | Database name inside each org namespace                          |

`SIBYL_SURREAL_URL` and `SIBYL_SURREAL_DATA_DIR` are mutually exclusive; set only one. In-memory
mode (`memory://`) is rejected in production.

## URL Configuration

| Variable             | Default                   | Description                                    |
| -------------------- | ------------------------- | ---------------------------------------------- |
| `SIBYL_PUBLIC_URL`   | `http://localhost:3337`   | Public base URL for OAuth callbacks, redirects |
| `SIBYL_SERVER_URL`   | (derived from public_url) | API base URL override                          |
| `SIBYL_FRONTEND_URL` | (derived from public_url) | Frontend base URL override                     |

When using Kong or similar ingress, `SIBYL_PUBLIC_URL` is typically set to the external domain
(e.g., `https://sibyl.example.com`), and both API and frontend are served from the same origin.

## Authentication

| Variable                            | Default | Description                                 |
| ----------------------------------- | ------- | ------------------------------------------- |
| `SIBYL_JWT_SECRET`                  | (empty) | **Required.** JWT signing secret            |
| `SIBYL_JWT_ALGORITHM`               | `HS256` | JWT signing algorithm                       |
| `SIBYL_ACCESS_TOKEN_EXPIRE_MINUTES` | `60`    | Access token TTL in minutes                 |
| `SIBYL_REFRESH_TOKEN_EXPIRE_DAYS`   | `30`    | Refresh token TTL in days                   |
| `SIBYL_DISABLE_AUTH`                | `false` | Disable auth enforcement (dev only)         |
| `SIBYL_MCP_AUTH_MODE`               | `auto`  | MCP auth: auto/on/off                       |
| `SIBYL_SETTINGS_KEY`                | (auto)  | Fernet key for encrypting DB-stored secrets |

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

## PostgreSQL

Used only by historical archive and migration commands that explicitly restore a retained
`postgres.sql` payload. Structured auth/content archive export now reads SurrealDB. PostgreSQL auth
and ambient runtime sidecars were removed after the v0.6.0 compatibility release; remove stale
`SIBYL_AUTH_STORE=postgres` values before starting the API.

| Variable                      | Default     | Description                       |
| ----------------------------- | ----------- | --------------------------------- |
| `SIBYL_POSTGRES_HOST`         | `localhost` | Migration sidecar host            |
| `SIBYL_POSTGRES_PORT`         | `5433`      | Migration sidecar port            |
| `SIBYL_POSTGRES_USER`         | `sibyl`     | Migration sidecar username        |
| `SIBYL_POSTGRES_PASSWORD`     | `sibyl_dev` | Migration sidecar password        |
| `SIBYL_POSTGRES_DB`           | `sibyl`     | Migration sidecar database name   |
| `SIBYL_POSTGRES_POOL_SIZE`    | `10`        | Migration sidecar connection pool |
| `SIBYL_POSTGRES_MAX_OVERFLOW` | `20`        | Migration sidecar overflow limit  |

Note: Port 5433 is the default for local migration sidecars to avoid conflicts with a local
PostgreSQL installation. Use the standard port 5432 only for external rehearsal databases that
already expose it.

## Redis/Valkey Coordination

Redis/Valkey is optional. The default Surreal runtime uses local in-process coordination.

| Variable               | Default     | Description            |
| ---------------------- | ----------- | ---------------------- |
| `SIBYL_REDIS_HOST`     | `127.0.0.1` | Redis/Valkey host      |
| `SIBYL_REDIS_PORT`     | `6381`      | Redis/Valkey port      |
| `SIBYL_REDIS_PASSWORD` | -           | Redis/Valkey password  |
| `SIBYL_REDIS_JOBS_DB`  | `1`         | Redis DB for job queue |

## LLM Configuration

| Variable                           | Default                  | Description                       |
| ---------------------------------- | ------------------------ | --------------------------------- |
| `SIBYL_LLM_PROVIDER`               | `anthropic`              | LLM provider: openai or anthropic |
| `SIBYL_LLM_MODEL`                  | `claude-haiku-4-5`       | LLM model for entity extraction   |
| `SIBYL_EMBEDDING_MODEL`            | `text-embedding-3-small` | OpenAI embedding model            |
| `SIBYL_EMBEDDING_DIMENSIONS`       | `1536`                   | Embedding vector dimensions       |
| `SIBYL_GRAPH_EMBEDDING_DIMENSIONS` | `1024`                   | Graph embedding dimensions        |

## API Keys

| Variable                  | Default | Description                              |
| ------------------------- | ------- | ---------------------------------------- |
| `SIBYL_OPENAI_API_KEY`    | (empty) | OpenAI API key (required for embeddings) |
| `SIBYL_ANTHROPIC_API_KEY` | (empty) | Anthropic API key                        |

### Lookup Priority

API keys are resolved in this order:

1. **Database** - Keys stored via web UI (Settings → AI Services)
2. **Environment variables** - `SIBYL_OPENAI_API_KEY`, `SIBYL_ANTHROPIC_API_KEY`
3. **Unprefixed fallbacks** - `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`

This allows zero-config deployments where API keys are entered through the onboarding wizard and
stored encrypted in the database (using `SIBYL_SETTINGS_KEY`).

### Unprefixed Fallbacks

- `OPENAI_API_KEY` -> `SIBYL_OPENAI_API_KEY`
- `ANTHROPIC_API_KEY` -> `SIBYL_ANTHROPIC_API_KEY`

## Native Memory Configuration

| Variable               | Default    | Description                                          |
| ---------------------- | ---------- | ---------------------------------------------------- |
| `SIBYL_RETRIEVAL_MODE` | `native`   | `graphiti`, `native`, or `compare` context retrieval |
| `SIBYL_NATIVE_WRITE`   | `disabled` | Enables direct native reflection promotion writes    |

`compare` mode returns native context results while logging policy-safe diffs against the
compatibility path. Set `SIBYL_RETRIEVAL_MODE=graphiti` only when you need the named compatibility
fallback for migration or investigation.

## Compatibility Graphiti Configuration

| Variable                         | Default | Description                              |
| -------------------------------- | ------- | ---------------------------------------- |
| `SIBYL_GRAPHITI_SEMAPHORE_LIMIT` | `10`    | Concurrent LLM operations limit          |
| `SEMAPHORE_LIMIT`                | (none)  | Alternative for Graphiti semaphore       |
| `GRAPHITI_TELEMETRY_ENABLED`     | `false` | Graphiti telemetry (disabled by default) |

## Email (Resend)

| Variable               | Default                     | Description                            |
| ---------------------- | --------------------------- | -------------------------------------- |
| `SIBYL_RESEND_API_KEY` | (empty)                     | Resend API key for transactional email |
| `SIBYL_EMAIL_FROM`     | `Sibyl <noreply@sibyl.dev>` | Default from address                   |

## Content Ingestion

| Variable                     | Default | Description                  |
| ---------------------------- | ------- | ---------------------------- |
| `SIBYL_CHUNK_MAX_TOKENS`     | `1000`  | Maximum tokens per chunk     |
| `SIBYL_CHUNK_OVERLAP_TOKENS` | `100`   | Token overlap between chunks |

## Worker Configuration

| Variable           | Default | Description                                                     |
| ------------------ | ------- | --------------------------------------------------------------- |
| `SIBYL_RUN_WORKER` | `false` | Embed the legacy Redis worker in the API process when supported |

## Example .env Files

### Local Development

```bash
# .env
SIBYL_ENVIRONMENT=development
SIBYL_JWT_SECRET=dev-secret-change-in-production

# Recommended local runtime
SIBYL_STORE=surreal
SIBYL_COORDINATION_BACKEND=local
SIBYL_SURREAL_URL=ws://127.0.0.1:8000/rpc
SIBYL_SURREAL_USERNAME=root
SIBYL_SURREAL_PASSWORD=root

# LLM
SIBYL_OPENAI_API_KEY=sk-...
SIBYL_ANTHROPIC_API_KEY=sk-ant-...

# Logging
SIBYL_LOG_LEVEL=DEBUG
```

### Production (Surreal, default)

```bash
SIBYL_ENVIRONMENT=production
SIBYL_JWT_SECRET=<generate with: openssl rand -hex 32>

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
SIBYL_RESEND_API_KEY=re_...
SIBYL_EMAIL_FROM=Sibyl <sibyl@example.com>
```

### Migration Archive Rehearsal

Use PostgreSQL settings only when explicitly restoring or validating a retained `postgres.sql`
payload. New production deployments should use the fully Surreal example above.

```bash
SIBYL_ENVIRONMENT=production
SIBYL_JWT_SECRET=<generate with: openssl rand -hex 32>
SIBYL_PUBLIC_URL=https://sibyl.example.com

# Surreal target
SIBYL_SURREAL_URL=ws://prod-surrealdb.internal:8000/rpc
SIBYL_SURREAL_USERNAME=root
SIBYL_SURREAL_PASSWORD=<secure-password>

# Optional migration/archive sidecar
SIBYL_POSTGRES_HOST=prod-postgres.internal
SIBYL_POSTGRES_PORT=5432
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

| Variable              | Default | Description                    |
| --------------------- | ------- | ------------------------------ |
| `SIBYL_SERVER_PORT`   | `3334`  | API/MCP server port            |
| `SIBYL_WEB_PORT`      | `3337`  | Web frontend port              |
| `SIBYL_SURREAL_PORT`  | `8000`  | SurrealDB port (default store) |
| `SIBYL_POSTGRES_PORT` | `5433`  | PostgreSQL sidecar port        |
| `SIBYL_BACKEND_URL`   | (auto)  | Backend URL for web app        |

### Quick Setup: Test Instance

1. Create `.env.test` with offset ports (copy from `.env.test.example`):

```bash
COMPOSE_PROJECT_NAME=sibyl-test
SIBYL_SERVER_PORT=3344
SIBYL_WEB_PORT=3347
SIBYL_POSTGRES_PORT=5443
SIBYL_POSTGRES_DB=sibyl_test
```

2. Start databases with isolated containers and volumes:

```bash
docker compose -p sibyl-test --env-file .env.test up -d
```

3. Start API pointing to test databases:

```bash
env $(cat .env.test | xargs) sibyld serve
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

- Use `docker compose -p sibyl-test ps` to see test instance containers
- Volumes are namespaced by project, for example `sibyl-test_surreal_data`
- CLI contexts let you switch between instances: `sibyl context use test`

## Computed Properties

The Settings class provides computed connection URLs:

```python
settings.resolved_surreal_url  # ws://..., surrealkv://..., or memory://
settings.postgres_url          # migration sidecar URL
settings.postgres_url_sync     # migration sidecar URL alias
settings.fully_surreal         # True when graph, content, and auth all use SurrealDB
settings.requires_relational_support  # False after v0.6.0 sidecar removal
```
