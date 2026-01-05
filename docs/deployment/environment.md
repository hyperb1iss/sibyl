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

| Variable                      | Default     | Description                          |
| ----------------------------- | ----------- | ------------------------------------ |
| `SIBYL_POSTGRES_HOST`         | `localhost` | PostgreSQL host                      |
| `SIBYL_POSTGRES_PORT`         | `5433`      | PostgreSQL port (5433 for local dev) |
| `SIBYL_POSTGRES_USER`         | `sibyl`     | PostgreSQL username                  |
| `SIBYL_POSTGRES_PASSWORD`     | `sibyl_dev` | PostgreSQL password                  |
| `SIBYL_POSTGRES_DB`           | `sibyl`     | PostgreSQL database name             |
| `SIBYL_POSTGRES_POOL_SIZE`    | `10`        | Connection pool size                 |
| `SIBYL_POSTGRES_MAX_OVERFLOW` | `20`        | Max overflow connections             |

Note: Port 5433 is the default for local development to avoid conflicts with a local PostgreSQL
installation. In Kubernetes, the standard port 5432 is used.

## FalkorDB

| Variable                  | Default       | Description                             |
| ------------------------- | ------------- | --------------------------------------- |
| `SIBYL_FALKORDB_HOST`     | `localhost`   | FalkorDB host                           |
| `SIBYL_FALKORDB_PORT`     | `6380`        | FalkorDB port (6380 for local dev)      |
| `SIBYL_FALKORDB_PASSWORD` | `conventions` | FalkorDB password                       |
| `SIBYL_REDIS_JOBS_DB`     | `1`           | Redis DB for job queue (0 = graph data) |

Note: Port 6380 is the default for local development to avoid conflicts with a local Redis
installation.

## LLM Configuration

| Variable                           | Default                  | Description                           |
| ---------------------------------- | ------------------------ | ------------------------------------- |
| `SIBYL_LLM_PROVIDER`               | `anthropic`              | LLM provider: openai or anthropic     |
| `SIBYL_LLM_MODEL`                  | `claude-haiku-4-5`       | LLM model for entity extraction       |
| `SIBYL_EMBEDDING_MODEL`            | `text-embedding-3-small` | OpenAI embedding model                |
| `SIBYL_EMBEDDING_DIMENSIONS`       | `1536`                   | Embedding vector dimensions           |
| `SIBYL_GRAPH_EMBEDDING_DIMENSIONS` | `1024`                   | Graph (Graphiti) embedding dimensions |

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

## Graphiti Configuration

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

| Variable           | Default | Description                     |
| ------------------ | ------- | ------------------------------- |
| `SIBYL_RUN_WORKER` | `false` | Embed arq worker in API process |

## Example .env Files

### Local Development

```bash
# apps/api/.env
SIBYL_ENVIRONMENT=development
SIBYL_JWT_SECRET=dev-secret-change-in-production

# Databases (Docker Compose ports)
SIBYL_POSTGRES_HOST=localhost
SIBYL_POSTGRES_PORT=5433
SIBYL_FALKORDB_HOST=localhost
SIBYL_FALKORDB_PORT=6380

# LLM
SIBYL_OPENAI_API_KEY=sk-...
SIBYL_ANTHROPIC_API_KEY=sk-ant-...

# Logging
SIBYL_LOG_LEVEL=DEBUG
```

### Production

```bash
SIBYL_ENVIRONMENT=production
SIBYL_JWT_SECRET=<generate with: openssl rand -hex 32>

# Public URL (Kong/ingress domain)
SIBYL_PUBLIC_URL=https://sibyl.example.com

# Databases
SIBYL_POSTGRES_HOST=prod-postgres.internal
SIBYL_POSTGRES_PORT=5432
SIBYL_POSTGRES_PASSWORD=<secure-password>
SIBYL_FALKORDB_HOST=prod-falkordb.internal
SIBYL_FALKORDB_PORT=6379
SIBYL_FALKORDB_PASSWORD=<secure-password>

# LLM
SIBYL_OPENAI_API_KEY=sk-...
SIBYL_ANTHROPIC_API_KEY=sk-ant-...
SIBYL_LLM_PROVIDER=anthropic
SIBYL_LLM_MODEL=claude-sonnet-4

# Rate limiting with Redis
SIBYL_RATE_LIMIT_STORAGE=redis://prod-redis.internal:6379

# Email
SIBYL_RESEND_API_KEY=re_...
SIBYL_EMAIL_FROM=Sibyl <sibyl@example.com>
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
  SIBYL_POSTGRES_PASSWORD: "<db-password>"
  SIBYL_FALKORDB_PASSWORD: "<falkordb-password>"
```

## Running Multiple Instances

You can run multiple Sibyl instances on the same machine (e.g., dev + test environments) by
configuring different ports and container names.

### Port Configuration

| Variable                      | Default | Description             |
| ----------------------------- | ------- | ----------------------- |
| `SIBYL_SERVER_PORT`           | `3334`  | API/MCP server port     |
| `SIBYL_WEB_PORT`              | `3337`  | Web frontend port       |
| `SIBYL_FALKORDB_PORT`         | `6380`  | FalkorDB port           |
| `SIBYL_FALKORDB_BROWSER_PORT` | `3335`  | FalkorDB Browser UI     |
| `SIBYL_POSTGRES_PORT`         | `5433`  | PostgreSQL port         |
| `SIBYL_BACKEND_URL`           | (auto)  | Backend URL for web app |

### Quick Setup: Test Instance

1. Create `.env.test` with offset ports (copy from `.env.test.example`):

```bash
COMPOSE_PROJECT_NAME=sibyl-test
SIBYL_SERVER_PORT=3344
SIBYL_WEB_PORT=3347
SIBYL_FALKORDB_PORT=6390
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

- `COMPOSE_PROJECT_NAME` isolates Docker containers and volumes (e.g., `sibyl-test-falkordb`)
- Each port variable controls the corresponding service
- `SIBYL_BACKEND_URL` tells the web frontend where to proxy API requests

### Tips

- Use `docker compose -p sibyl-test ps` to see test instance containers
- Volumes are namespaced by project: `sibyl-test_falkordb_data` vs `sibyl_falkordb_data`
- CLI contexts let you switch between instances: `sibyl context use test`

## Computed Properties

The Settings class provides computed connection URLs:

```python
settings.falkordb_url  # redis://:password@host:port
settings.postgres_url  # postgresql+asyncpg://user:pass@host:port/db
settings.postgres_url_sync  # postgresql://user:pass@host:port/db (for Alembic)
```
