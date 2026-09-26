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

`auto` resolves to local in-process coordination unless Redis settings are present. Use `redis` for
multi-pod deployments. See [Storage Modes](../guide/storage-modes.md) for the connection options.

## SurrealDB

SurrealDB is the default and only runtime store. These settings apply to every Sibyl process.

| Variable                         | Default | Description                                                           |
| -------------------------------- | ------- | --------------------------------------------------------------------- |
| `SIBYL_SURREAL_URL`              | (empty) | Connection URL; see [URL forms](#surrealdb-url-forms) below           |
| `SIBYL_SURREAL_DATA_DIR`         | (empty) | Local SurrealKV path used when `SIBYL_SURREAL_URL` is unset           |
| `SIBYL_SURREAL_USERNAME`         | (empty) | Root username for remote runtimes                                     |
| `SIBYL_SURREAL_PASSWORD`         | (empty) | Root password for remote runtimes                                     |
| `SIBYL_SURREAL_TOKEN`            | (empty) | Bearer token for remote runtimes (alternative to username/password)   |
| `SIBYL_SURREAL_NAMESPACE_PREFIX` | `org_`  | Namespace prefix for per-org isolation (`org_<uuid_hex>`)             |
| `SIBYL_SURREAL_DATABASE`         | `graph` | Database name inside each org namespace                               |
| `SIBYL_SURREAL_SLOW_QUERY_MS`    | `500`   | Log SurrealDB queries at warning level when elapsed time exceeds this |

### Connection Pooling

| Variable                                | Default     | Description                                          |
| --------------------------------------- | ----------- | ---------------------------------------------------- |
| `SIBYL_SURREAL_POOL_SIZE`               | `8`         | Concurrent connections per dedicated client (1-256)  |
| `SIBYL_SURREAL_AUTH_POOL_SIZE`          | (pool size) | Override for the auth client pool                    |
| `SIBYL_SURREAL_CONTENT_POOL_SIZE`       | (pool size) | Override for the content client pool                 |
| `SIBYL_SURREAL_GRAPH_POOL_SIZE`         | (pool size) | Override for org graph client pools                  |
| `SIBYL_SURREAL_GRAPH_CLIENT_CACHE_SIZE` | `64`        | Org-scoped graph clients kept open per process (LRU) |
| `SIBYL_ALLOW_EMBEDDED_SINGLE_WRITER`    | `false`     | Allow file-backed embedded storage in production     |

`SIBYL_SURREAL_URL` and `SIBYL_SURREAL_DATA_DIR` are mutually exclusive; set only one. When neither
is set, Sibyl falls back to in-memory mode (`memory://`), which is rejected in production.

### SurrealDB URL forms

| Form                                                | Runs            | Production                                    |
| --------------------------------------------------- | --------------- | --------------------------------------------- |
| `ws://`, `wss://`, `http://`, `https://`            | Remote server   | Allowed                                       |
| `surrealkv://`, `surrealkv+versioned://`, `file://` | Embedded, files | Requires `SIBYL_ALLOW_EMBEDDED_SINGLE_WRITER` |
| `memory://`, `mem://`                               | Embedded, RAM   | Rejected                                      |

Schemes are case-insensitive. sibyld refuses any other scheme at startup in every environment,
including `rocksdb://`, `tikv://`, `surrealdb://`, and a bare `host:port`. `rocksdb://` is a storage
argument for `surreal start`, not a client URL: run a SurrealDB server on RocksDB and point
`SIBYL_SURREAL_URL` at its `ws://` endpoint. The `sibyl` client CLI does not validate this setting,
since it never opens the store itself.

Embedded URLs clamp every pool to a single connection. The embedded engine misses concurrent
write-write conflicts, so each namespace must write through one connection. Running file-backed
embedded storage in production also requires the explicit `SIBYL_ALLOW_EMBEDDED_SINGLE_WRITER=1`
opt-in, and only when one daemon owns the database; otherwise startup fails validation.

Sibyl redacts this URL wherever it reports a connection. Connection errors, logs, and status
payloads (`sibyl debug status` and the admin health payload) show only the scheme and, for a server,
its host and port; userinfo, path, query, and fragment are left out, so an error you paste into an
issue carries no secret from `SIBYL_SURREAL_URL`. The embedded daemon's startup banner is the one
place a path appears: `sibyld serve --embedded` prints its own data directory.

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
| `SIBYL_LOCAL_AUTH_ENABLED`          | `false`    | Enable local username/password login after setup        |
| `SIBYL_PUBLIC_SIGNUPS_ENABLED`      | `false`    | Allow public self-serve account creation after setup    |
| `SIBYL_OIDC`                        | `{}`       | JSON object for optional OIDC providers and session UX  |
| `SIBYL_BREAK_GLASS_ENABLED`         | `false`    | Enable bounded emergency local login for SSO outages    |
| `SIBYL_BREAK_GLASS_ALLOWED_IPS`     | `[]`       | JSON array of CIDRs allowed to use break-glass login    |
| `SIBYL_BREAK_GLASS_EXPIRES_AT`      | (empty)    | UTC expiry for break-glass, no more than four hours out |

`SIBYL_LOCAL_AUTH_ENABLED` defaults to `false` and is auto-enabled only when
`SIBYL_ENVIRONMENT=development` and the variable is unset. Production deployments that want local
username/password login must set it explicitly. The Helm chart (`auth.localAuthEnabled: true`) and
the Ansible self-host stack enable it by default; `docker-compose.prod.yml` leaves it `false`. The
local-first single-user flow is otherwise unchanged: the first setup signup creates the owner/admin
user, and account creation after setup is invite-based unless `SIBYL_PUBLIC_SIGNUPS_ENABLED=true`.

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

Anonymous requests are limited per client address, so behind a reverse proxy set
`SIBYL_FORWARDED_ALLOW_IPS` below. Without it every user shares the proxy's budget.

## Trusted Proxies

| Variable                    | Default         | Description                                      |
| --------------------------- | --------------- | ------------------------------------------------ |
| `SIBYL_FORWARDED_ALLOW_IPS` | `127.0.0.1,::1` | Comma-separated IPs and CIDRs of trusted proxies |

Behind a reverse proxy, ingress controller, or load balancer, every request reaches Sibyl from the
proxy's address. Anything keyed on the client address then treats all users as one client: the login
route allows five attempts per minute per address, so the sixth sign-in in a minute from anyone
locks everyone out. List the proxies in `SIBYL_FORWARDED_ALLOW_IPS` and Sibyl takes the client
address from `X-Forwarded-For` instead.

Set it when a proxy sits between users and the backend: the Helm chart
(`backend.forwardedAllowIps`), the Ansible stack (`sibyl_forwarded_allow_ips`, which trusts Caddy by
default), or your own nginx or Caddy in front of `docker-compose.prod.yml`. Leave it unset when
clients connect to the backend port directly.

With `docker-compose.prod.yml`, run the proxy as a container on the same network, list only its
address, and stop publishing port 3334 (or bind it to `127.0.0.1`). Docker's userland proxy delivers
host-loopback and IPv6 connections to a published port from the bridge gateway, so trusting a bridge
range there would let those clients choose their address.

**How the address resolves.** Uvicorn applies the list before Sibyl sees a request. When the direct
peer is in the list, it reads `X-Forwarded-For` from right to left and takes the first entry that is
not itself in the list; when every entry is trusted, it takes the leftmost. When the direct peer is
not in the list, the header is ignored. For example, with
`SIBYL_FORWARDED_ALLOW_IPS=10.250.0.0/28,10.0.0.0/28`, a request from ingress pod `10.250.0.7`
carrying `X-Forwarded-For: 203.0.113.99, 198.51.100.10, 10.0.0.5` resolves to `198.51.100.10`: the
load balancer at `10.0.0.5` is trusted and skipped, and the client-written `203.0.113.99` is never
reached. That one resolved address feeds the per-address rate limits, request logs, session and
audit records, and the break-glass allowlist. Trusted peers also set the request scheme through
`X-Forwarded-Proto`; Sibyl builds its links from `SIBYL_PUBLIC_URL`, so nothing visible changes.

**Accepted values.**

- Unset keeps the loopback default (`127.0.0.1` and `::1`), unless uvicorn's own
  `FORWARDED_ALLOW_IPS` variable is set, in which case its list applies.
- Set but empty (or only commas and spaces) means loopback only. Once the Sibyl variable exists in
  the environment, even empty, `FORWARDED_ALLOW_IPS` is ignored.
- A configured list replaces the loopback default rather than adding to it. Include `127.0.0.0/8` if
  a proxy in the same pod or on the same host also fronts the backend; some sidecar proxies connect
  from loopback addresses other than `127.0.0.1`.
- Entries are comma-separated, not a JSON array (unlike `SIBYL_BREAK_GLASS_ALLOWED_IPS`). Each must
  be an IP address or a CIDR range written with its network address (`10.20.0.0/16`, not
  `10.20.3.4/16`). Anything else, a hostname included, fails startup, because uvicorn would
  otherwise ignore the entry and leave every login on the proxy's budget.
- A lone `*` trusts every peer and cannot be combined with other entries. Uvicorn then takes the
  leftmost entry, which the client writes unless every proxy in front overwrites the header, so any
  client can choose its own address. Sibyl logs `forwarded_allow_ips_trusts_every_peer` at startup
  when it is set. The same warning fires for any range broader than an IPv4 `/8` or an IPv6 `/32`.
  That covers a `/0` such as `0.0.0.0/0` and split halves such as `0.0.0.0/1,128.0.0.0/1`, either of
  which trusts a whole address family the way `*` does.

::: warning Security note

Trusting a range lets anything inside it choose its client address. A peer in the list can claim to
be any client, sidestep the per-address rate limits, and satisfy `SIBYL_BREAK_GLASS_ALLOWED_IPS`.
Every address in the list must be a proxy, never something that could be a client. Keep the list to
the proxies that actually front Sibyl, make sure nothing else in those ranges can reach the backend
port (a NetworkPolicy, no published port), and only trust a hop that writes the address it saw into
`X-Forwarded-For`. A proxy that appends to the header rather than replacing it (Envoy-based
gateways, AWS ALB, nginx's `$proxy_add_x_forwarded_for`) also lets any caller whose own address is
trusted forge an entry through it, so a pod CIDR, node range, or VPC CIDR is never safe to list,
NetworkPolicy or not. Prefer a proxy that replaces the header from untrusted peers. A hop that only
forwards packets, such as nodes that SNAT or an L4 load balancer without source preservation, must
not be trusted: trusting it gives the next entry, which the client wrote, the final say. The Next.js
frontend keeps a client-supplied `X-Forwarded-For`, so route `/api` and `/mcp` straight to the
backend (the Helm and Caddy defaults do) and never count the frontend as a trusted hop.

:::

If you allowlisted the proxy's range in `SIBYL_BREAK_GLASS_ALLOWED_IPS` because that was the only
address the backend saw, replace it with the operators' real addresses when you set this. To confirm
the setting took, sign in through the proxy and check that the backend's `request` log lines carry
your own address in `client`.

## Redis/Valkey Coordination

Redis/Valkey is optional. The default Surreal runtime uses local in-process coordination.

| Variable               | Default     | Description            |
| ---------------------- | ----------- | ---------------------- |
| `SIBYL_REDIS_HOST`     | `127.0.0.1` | Redis/Valkey host      |
| `SIBYL_REDIS_PORT`     | `6381`      | Redis/Valkey port      |
| `SIBYL_REDIS_PASSWORD` | -           | Redis/Valkey password  |
| `SIBYL_REDIS_JOBS_DB`  | `1`         | Redis DB for job queue |

## LLM Configuration

| Variable                           | Default            | Description                                      |
| ---------------------------------- | ------------------ | ------------------------------------------------ |
| `SIBYL_LLM_PROVIDER`               | `anthropic`        | LLM provider: anthropic, bedrock, gemini, openai |
| `SIBYL_LLM_MODEL`                  | `claude-haiku-4-5` | LLM model for entity extraction                  |
| `SIBYL_LLM_TIMEOUT_SECONDS`        | `60`               | Per-attempt read timeout                         |
| `SIBYL_LLM_MEMORY_TIMEOUT_SECONDS` | `600`              | Per-attempt read timeout for the memory surface  |

Any `SIBYL_LLM_*` setting also takes a per-surface form, `SIBYL_LLM_<SURFACE>_<SETTING>`, for the
`DEFAULT`, `CRAWLER`, `MEMORY` and `SYNTHESIS` surfaces, and the surface form wins. Consolidation
runs on the memory surface and sends a whole cohort in one non-streaming request, so that surface
waits ten minutes per attempt rather than one.

### Amazon Bedrock

The `bedrock` provider serves Claude through Amazon Bedrock, and the same settings back Cohere Embed
v4 when an embedding provider is `bedrock`. It needs no API key: requests sign with the default AWS
credential chain, which covers IRSA web identity and EKS Pod Identity on Kubernetes, SSO profiles,
static keys and instance roles.

| Variable                        | Default  | Description                                                                                  |
| ------------------------------- | -------- | -------------------------------------------------------------------------------------------- |
| `SIBYL_BEDROCK_REGION`          | (unset)  | Bedrock Region; falls back to `AWS_REGION`, then `AWS_DEFAULT_REGION`                        |
| `SIBYL_BEDROCK_INFERENCE_SCOPE` | `us`     | Geographic profile (`us`, `eu`, `apac`, `jp`, `au`, `ca`, `us-gov`), `global`, or `regional` |
| `SIBYL_BEDROCK_API`             | `invoke` | `invoke` (InvokeModel on bedrock-runtime) or `mantle` (bedrock-mantle)                       |
| `SIBYL_BEDROCK_PROFILE`         | (unset)  | AWS profile for local development                                                            |
| `SIBYL_BEDROCK_API_KEY`         | (unset)  | Bedrock API key, sent as a bearer token instead of SigV4 signing                             |

A region is required. Without one, every Bedrock LLM call fails with a message naming these
variables, and Bedrock embeddings stay off the way a missing API key turns off the other providers.
Any other invalid Bedrock setting raises wherever it is used. `SIBYL_BEDROCK_API_KEY` falls back to
`AWS_BEARER_TOKEN_BEDROCK`, and it cannot be combined with `SIBYL_BEDROCK_PROFILE`. On `mantle`, the
Claude client also takes `ANTHROPIC_AWS_API_KEY` when no profile is set, because its SDK client
reads it; Cohere requests never use that key. The Anthropic SDK reads these variables on its own, so
a stray value in the environment replaces SigV4 signing.

The scope defaults to `us` whatever the Region, so a deployment outside the US sets it explicitly:
`eu` for an EU Region, for example.

Configure models by their Claude alias, as on the `anthropic` provider. Sibyl maps the alias to the
Bedrock ID through the inference scope, so `claude-opus-5-5` becomes `us.anthropic.claude-opus-5-5`
under `us` and `global.anthropic.claude-opus-5-5` under `global`. An ID that already names an
inference profile, or any Bedrock ARN, is sent as given. Most current Claude models and Cohere Embed
v4 offer no in-Region on-demand throughput, so `regional` only works where the model card lists
In-Region support. Effort, memory-surface defaults and the forced-tool rule all key by alias, so a
raw Bedrock ID, or an inference-profile or foundation-model ARN, behaves exactly like its alias. An
application inference profile or provisioned throughput ARN hides the model behind it, so it gets
none of those Claude-specific rules; route Opus 5 and Opus 5.5 through an inference profile ID
instead.

Bedrock rejects native structured output (`output_config.format`) for Claude Opus 4.8, Opus 5, Opus
5.5 and Sonnet 5, and for every model on bedrock-mantle. On those models Sibyl uses tool output
instead, and Opus 5.5, which also refuses a forced tool choice, asks with `tool_choice: auto`. An
explicit `SIBYL_CONSOLIDATION_OUTPUT_MODE=native_strict` fails before any request on those routes.
Opus 5.5 keeps its 1M-token context window on Bedrock with no beta header.

The IAM role needs `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on each
inference profile it routes through and on the foundation models behind it in every destination
Region, for example `arn:aws:bedrock:*:<account>:inference-profile/us.anthropic.claude-*`,
`arn:aws:bedrock:*::foundation-model/anthropic.claude-*` and the matching `cohere.embed-v4:0`
resources. The `mantle` API takes `bedrock-mantle:CreateInference` instead, serves in-Region IDs
only, and runs in fewer Regions, so `invoke` is the default.

The provider **Test** button and `/api/settings/ai/keys/bedrock/test` first prove a region and
credentials resolve, then make one minimal Claude Haiku 4.5 call. A missing region or credential
reports `missing_credentials`, and an unknown model reports `model_not_found`.

### Consolidation Input Budget

| Variable                              | Default           | Description                                                                      |
| ------------------------------------- | ----------------- | -------------------------------------------------------------------------------- |
| `SIBYL_CONSOLIDATION_MAX_INPUT_CHARS` | unset (per model) | Character cap on one consolidation request: system, evidence, schema             |
| `SIBYL_CONSOLIDATION_RUN_MAX_TOKENS`  | `10000000`        | Token ceiling one reflection dream run may reserve across all of its model calls |

Unset, each request takes the memory model's own budget: 1,600,000 characters for `claude-opus-5`
and `claude-opus-5-5` on the `anthropic` and `bedrock` providers, and 40,000 for every other model.
The lookup matches the model alias exactly, so a dated id, a `[1m]` suffix or an
`anthropic/`-prefixed OpenRouter id gets 40,000, while a Bedrock ID such as
`us.anthropic.claude-opus-5-5` counts as its alias. When the variable is set, its value replaces the
model's budget, even a value equal to a default.

At the Opus default one consolidation request can carry about 420K input tokens (screen48 evidence
ran about 3.8 characters per token). A lower value shrinks every such request and splits large task
families into more, smaller cohorts. The budget is recorded in each validation policy, so changing
it, or changing the memory model, re-sends consolidation work that was in flight or only partly
complete.

### Dream Run Spend

The nightly dream cycle's proposal, critique and correction calls reserve against the monthly LLM
token budgets of the source's owner and the organization, the same buckets the extraction jobs use,
and each call settles to the tokens it used once it returns, in the month it was reserved. On the
Anthropic and OpenAI transports a call reserves one attempt up front and each retry reserves another
as it dispatches, so a call that succeeds first time never holds its full retry envelope. Other
providers reserve every output attempt up front, since their retries are not observed one by one.

A refusal that arrives before a request is sent releases that stage instead of failing it, so a
proposal, critique or correction the budget refused runs again once there is room. The refusal is
listed in the run report with its budget details, and the run continues with the next cohort.

`SIBYL_CONSOLIDATION_RUN_MAX_TOKENS` caps what one run may reserve in total. When the next
reservation would cross it, that call is refused, the run stops admitting cohorts and candidates,
and the receipt records `stopped_reason: run_token_ceiling` with the reserved, refunded and
committed totals under `spend`. Cohorts the run did not reach stay pending and are picked up when
the dream walk next reaches them; pending candidates are drained on the next run. Individual
reflection passes make no model call and are not stopped by the ceiling. At the Opus input budget a
joined cohort's proposal and critique reserve about 0.9M tokens, so the default admits about ten
such cohorts a night; at a 40,000-character budget a call reserves about 11K tokens and the default
never binds.

## Embeddings

Document chunk embeddings and graph node/relationship embeddings are configured separately. The
graph embedding dimensions also size the native Surreal vector indexes.

| Variable                           | Default                  | Description                                                   |
| ---------------------------------- | ------------------------ | ------------------------------------------------------------- |
| `SIBYL_EMBEDDING_PROVIDER`         | `openai`                 | Document chunk embedding provider: openai, gemini, or bedrock |
| `SIBYL_EMBEDDING_MODEL`            | `text-embedding-3-small` | Document chunk embedding model                                |
| `SIBYL_EMBEDDING_DIMENSIONS`       | `1536`                   | Document chunk embedding vector dimensions                    |
| `SIBYL_GRAPH_EMBEDDING_PROVIDER`   | `openai`                 | Graph embedding provider: openai, gemini, local, or bedrock   |
| `SIBYL_GRAPH_EMBEDDING_MODEL`      | `text-embedding-3-small` | Graph node/relationship embedding model                       |
| `SIBYL_GRAPH_EMBEDDING_DIMENSIONS` | `1024`                   | Graph embedding dimensions (sizes vector indexes)             |

The `local` graph embedding provider runs sentence-transformers models in-process with no API key.
When `SIBYL_GRAPH_EMBEDDING_PROVIDER=local` and no model is set, the model defaults to
`sentence-transformers/all-MiniLM-L6-v2` and the dimensions are derived from the model. Document
chunk embeddings (`SIBYL_EMBEDDING_PROVIDER`) support `openai`, `gemini` and `bedrock`.

The `bedrock` provider embeds with Cohere Embed v4 (`cohere.embed-v4:0`, the default when the model
is left at the OpenAI default) through the [Amazon Bedrock](#amazon-bedrock) settings above, and the
inference scope routes it like Claude. Queries embed as `search_query` and documents as
`search_document`. The dimensions must be 256, 512, 1024 or 1536, so the defaults of 1536 for
document chunks and 1024 for graph vectors both fit the existing indexes. Vectors record provider
`bedrock` and model `cohere.embed-v4:0` whichever scope routed them. Requests batch at up to 96
texts and about 16 MB each and run concurrently, and throttling retries with jittered backoff.

### Changing the Embedding Model

Every stored vector records the provider, model and dimensions that produced it. When the graph or
document chunk embedding settings change, the lifecycle repair job (every minute) re-embeds each
vector whose recorded model differs from the configured one. It works in time-budgeted passes that
resume where they stopped, including after a restart, and `sibyl debug status` shows each plane's
progress. Until a row is re-embedded, vector search skips it and lexical search still finds it.

A vector is replaced only when a field that shapes it changes: the provider, the model, the
dimensions, the embedded text format, or whether the provider embeds documents and queries
differently. Bookkeeping fields such as the embedding cache namespace never trigger a re-embed.

Embedding settings take effect when a process starts. Every API and worker process reads the
environment first and a value saved in the settings UI second. Graph search, document search, the
crawler, raw captures and the sweep all resolve the model this way, so they never disagree about
which model is configured. A change saved in the settings UI reaches each process at its next
restart, and an environment variable always overrides it. After changing either, restart every API
and worker process.

A graph dimension change rebuilds the graph vector indexes at startup, and the sweep then
regenerates the cleared vectors. The document chunk vector field is sized once from
`SIBYL_EMBEDDING_DIMENSIONS`, so a chunk dimension change reports `skipped_dimension_mismatch`
instead of re-embedding until that field is rebuilt.

#### Upgrading and switching providers in the same deploy

If you upgrade to this release and change the embedding provider or model in the same deploy, do one
of these:

- Set `SIBYL_EMBEDDING_LEGACY_VECTORS=reembed` for that deploy. Every vector written before this
  release is re-embedded with the new model.
- Deploy twice. Upgrade on the old provider, wait until `sibyl debug status` shows every plane
  `complete`, then switch providers in a second deploy. Every vector then carries a record of its
  model and the switch re-embeds exactly the stale ones.

Either way, restart or replace every API and worker process in the deploy that changes the model:
pods from the previous release left writing under a different model than the new ones write vectors
Sibyl cannot tell apart from the old ones.

Without either step Sibyl still decides from what the store held before the upgrade, and in most
deployments it decides correctly. Vectors written before Sibyl recorded their model are classified
once per organization and plane, and the verdict is stored. With the default `auto`, a plane is
re-embedded when any of these name a model other than the configured one:

- the recorded models on the plane's own vectors (graph vectors have recorded one for several
  releases);
- for document chunks, which did not record a model before this release, the content embedding
  settings saved in the settings UI, or where none were saved the recorded models on raw captures
  anywhere in the deployment. The previous release's crawler read a saved setting before the
  environment while raw captures read the environment first, so a saved setting is what embedded the
  chunks, and raw captures on another model do not override it;
- another organization's graph vectors, since one configuration embeds every organization;
- the models earlier lifecycle passes of this release ran with;
- the other plane's evidence, since a deployment that moved one provider has usually moved both.

Any of these outranks evidence that the model is unchanged: when the evidence conflicts, the plane
is re-embedded. With no switch in sight, a plane whose own vectors record the configured model
adopts its vectors in place, with no provider calls and no warning.

A plane with no recorded model of its own, such as an organization whose graph vectors all predate
model records, adopts on another organization's evidence when that evidence names the configured
model. `sibyl debug status` then shows the notice `adopted_on_deployment_evidence` instead of
`complete`. This trusts one organization's record for another's unrecorded vectors, which holds
because one configuration embeds every organization. It is wrong only if that organization's vectors
came from somewhere else, for example an import of vectors embedded outside this deployment; in that
case run `sibyld db reembed --org-id <id>`.

Only evidence that existed before this release first touched the store counts. Each schema upgrade
takes a snapshot of it, so vectors written after the restart are never mistaken for proof. Every
model record this release writes carries a format version and the snapshots count only records
without one, so a row the new release wrote before its migration ran is not mistaken for proof
either. Only a recorded model beside an actual vector counts, so a model name supplied with a row
that was never embedded proves nothing. Nothing that reads or rewrites this evidence runs before its
schema has upgraded: a worker that starts before the API has migrated the content schema skips the
embedding sweep and raw capture repair, and `sibyl debug status` shows `awaiting_schema_upgrade`,
until the migration has run. The model record is written only after a lifecycle pass has swept under
a configuration without the provider refusing it, so a process that merely started with a wrong
configuration leaves no trace.

Sibyl writes model records itself. A record sent with a write through the API or the MCP tools is
discarded and replaced by the server's own, and only restored archives bring their records in.

Earlier releases accepted a model record sent with a write, and one sent that way cannot be told
apart from a genuine one. Records on one organization's graph count as evidence for every
organization, so a user of release 1.4.1 or earlier who wrote a record naming some other model could
make other organizations' planes re-embed after the upgrade. That costs provider calls; it does not
put wrong vectors in search. A forged record naming the model the deployment will run next can at
most make a plane with no record of its own adopt its vectors, and that plane then shows
`adopted_on_deployment_evidence` in status, never `complete`. `sibyl debug status` names each
plane's basis, so a re-embed decided by another organization's records reads
`deployment_stamps_differ`, and `sibyld db reembed --dry-run` counts, per organization and plane,
the rows a re-embed replaces, which is the most such a record can cost. If users you do not trust
could write to the deployment before this upgrade, set `SIBYL_EMBEDDING_LEGACY_VECTORS=reembed` for
the upgrade deploy and every plane is re-embedded regardless of records.

A plane whose verdict would adopt its vectors, and a plane with no evidence of its own, waits
(`awaiting_evidence` in status) until every organization has published its graph evidence: an
organization that has not published yet may hold the recorded models that show a switch, so the
verdict never depends on which organization the scheduler reached first. In a healthy deployment
every organization publishes during the first lifecycle pass, and the waiting planes are settled at
its end. A plane whose evidence already shows a switch is re-embedded without waiting. An
organization counts once it has published in any pass. If one cannot publish, for example because
its graph namespace is unreachable, the logs name it (status names it to deployment admins and
counts it for everyone else), and the waiting planes are settled on the evidence published so far
once `SIBYL_EMBEDDING_SWEEP_EVIDENCE_WAIT_SECONDS` have passed. Status and that log line say how
many seconds remain until then.

A plane with no evidence anywhere adopts its vectors too, logs a warning, and shows
`adopted_without_evidence` in `sibyl debug status` instead of `complete`. That state is exactly what
an undetected switch would look like, which is why the steps above exist. If the model did change,
replace the vectors:

```bash
sibyld db reembed --dry-run                                  # rows each organization would re-embed
sibyld db reembed --org-id <id> --plane graph|documents|all  # mark them; the sweep re-embeds them
```

`sibyld db reembed` only writes metadata; the sweep does the embedding on its next passes, and the
warning clears.

#### Upgrade time

The migrations that take the evidence snapshot scan existing rows once. The content upgrade scans
raw captures and builds an index on them before the API answers requests, about 16 seconds per
100,000 rows on a native SurrealDB 3.2 server, so a million raw captures add about three minutes to
that start. Each organization's graph upgrade scans its entities and relationships, about 5.4
seconds per 100,000 entities, the first time the organization is used after the upgrade. The Helm
chart's backend `startupProbe` allows 10 minutes before liveness checks begin; raise its
`failureThreshold` for larger stores so a long migration is not killed and restarted.

Adopting vectors and rewriting raw capture records in the current format are metadata-only and
finish in the first pass that reaches them, whatever the pass budget: about 1,600 vectors a second
on a native 3.2 server (100,000 graph vectors in 64 seconds), and a 20,000-capture organization's
raw captures in under a minute. Vector search counts the unstamped vectors from the moment the adopt
verdict is recorded, so adoption itself costs no vector results.

| `SIBYL_EMBEDDING_LEGACY_VECTORS` | Vectors written before Sibyl recorded their model are                                                      |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `auto` (default)                 | Re-embedded when the evidence above names another model, adopted otherwise (with a warning if it is empty) |
| `adopt`                          | Adopted as the configured model                                                                            |
| `reembed`                        | Re-embedded                                                                                                |

Bedrock model IDs record the model without their routing: `us.`, `eu.`, `apac.` and `global.`
prefixes, foundation-model ARNs and inference-profile ARNs all record the same model, so moving
between them re-embeds nothing. An application inference profile ARN does not name its model, so it
is recorded as the full ARN. Moving between one and a model ID re-embeds every vector.

Restored archives keep each vector's recorded model. Vectors restored without one are marked
unverified and re-embedded.

#### Search while vectors are re-embedded

Vector search only compares a query with vectors from the query's model. A vector from before
stamping counts as the configured model once its plane's verdict to adopt it is recorded, which in a
healthy deployment happens at the end of the first lifecycle pass. Until then such vectors are
reached only through lexical search: a verdict still pending may yet turn to re-embed on another
organization's evidence, so counting them early could score an old model's vectors against a new
model's queries. Once the model has changed (a plane last finished for another model, or its vectors
from before stamping were judged another model's), right after the switch almost none qualify, and a
vector lane that has to pass over nearly every stored vector to find a few is slow and finds little.
So while fewer than 5% of a switched plane's vectors are in the configured model, graph search and
document hybrid search skip their vector lane and answer from lexical search. A plane that never
switched never skips. Retrieval diagnostics report the vector status as `vector_lane_model_switched`
or `vector_lane_model_sparse`, and the log records `vector_lane_skipped`. Each process re-reads the
sweep's progress at most every 30 seconds, so the lane comes back on its own as the sweep converts
vectors, and is fully back when the plane completes. The threshold comes from lanes measured on a
native SurrealDB 3.2 server at 20,000 entities: at a fresh switch each lane took 1 to 2 seconds and
found nothing, with 1% of vectors converted 0.3 to 1 second, from 5% 0.1 to 0.5 seconds, and 13 to
40 milliseconds once the sweep had finished. The vector-only document endpoints (`/api/rag/search`
and `/api/rag/code-examples`) have no lexical lane to fall back on, so they always run, and after an
upgrade or a switch they return nothing for chunks their plane does not count yet: chunks from
before stamping until the verdict is recorded, and old-model chunks until the sweep replaces them.
`/api/rag/hybrid-search` still finds those chunks lexically.

Each pass holds a lease on its plane and checks it in the same statement as every vector write, so a
pass that loses its lease to another process, for example after a long provider stall, writes
nothing and stops. A pass also stops starting provider requests once its time budget is spent. Raw
capture repair holds no lease; each of its writes lands only if the capture still carries the record
it read, so two processes configured for different models, as in a rolling deploy, cannot leave one
model's vector labeled with the other's.

A row whose text the provider refuses is remembered and not sent again until its text or the
configured model changes, an import reopens the plane, or `sibyld db reembed` runs; status counts it
as refused by the provider rather than pending.

| Variable                                        | Default | Description                                                         |
| ----------------------------------------------- | ------- | ------------------------------------------------------------------- |
| `SIBYL_EMBEDDING_SWEEP_BUDGET_SECONDS`          | `45`    | Time one lifecycle pass spends re-embedding one plane               |
| `SIBYL_EMBEDDING_SWEEP_PAGE_SIZE`               | `256`   | Rows read per page (raised to batch size times concurrency)         |
| `SIBYL_EMBEDDING_SWEEP_BATCH_SIZE`              | `96`    | Texts sent to the provider per request                              |
| `SIBYL_EMBEDDING_SWEEP_CONCURRENCY`             | `4`     | Most requests in flight; halves on provider throttling, then climbs |
| `SIBYL_EMBEDDING_SWEEP_VERIFY_INTERVAL_SECONDS` | `3600`  | How long a finished plane skips its table walk                      |
| `SIBYL_EMBEDDING_SWEEP_EVIDENCE_WAIT_SECONDS`   | `600`   | Longest a waiting plane waits for other organizations' evidence     |

## Retrieval Tuning

Vector index and reranking knobs. Changing HNSW parameters affects newly built indexes.

| Variable               | Default                                | Description                                             |
| ---------------------- | -------------------------------------- | ------------------------------------------------------- |
| `SIBYL_GRAPH_HNSW_EFC` | `150`                                  | Surreal HNSW graph index EF-construction value          |
| `SIBYL_GRAPH_HNSW_M`   | `12`                                   | Surreal HNSW index max connections per element          |
| `SIBYL_GRAPH_KNN_EF`   | `40`                                   | Floor on Surreal KNN query effort for graph vectors     |
| `SIBYL_RERANK_ENABLED` | `false`                                | Cross-encoder reranking after RRF fusion                |
| `SIBYL_RERANK_MODEL`   | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-encoder model for reranking                       |
| `SIBYL_RERANK_TOP_K`   | `20`                                   | Top candidates to rerank; the rest pass through (1-100) |

`SIBYL_GRAPH_KNN_EF` is a floor on search effort, not a ceiling. A Surreal HNSW read returns at most
`ef` rows, so retrieval raises the effective effort to the candidate count a lane asked for whenever
that pool runs deeper than the configured value. Lowering the setting therefore never truncates a
result set; raising it only improves recall for shallow reads.

Reranking requires the optional `reranking` extra (sentence-transformers). When the extra is not
installed, the path degrades cleanly to the fused order instead of raising.

## API Keys

| Variable                  | Default | Description                        |
| ------------------------- | ------- | ---------------------------------- |
| `SIBYL_OPENAI_API_KEY`    | (empty) | OpenAI API key (LLM or embeddings) |
| `SIBYL_ANTHROPIC_API_KEY` | (empty) | Anthropic API key                  |
| `SIBYL_GEMINI_API_KEY`    | (empty) | Gemini API key (LLM or embeddings) |

The `bedrock` provider needs none of these: it signs with AWS credentials, or a Bedrock API key from
`SIBYL_BEDROCK_API_KEY`. That key comes from the environment only and is never stored in the
database.

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

Reflection persistence always uses the native writer. Sibyl 1.4 removes the `SIBYL_NATIVE_WRITE`
switch; remove it from deployment configuration. The old `disabled` value selected a compatibility
writer and did not disable persistence. Use `persist=false` on reflection requests to preview
candidates without writing, or `persist_review=true` with `persist=true` to store candidates for
review before promotion.

| Variable                                         | Default | Description                                                |
| ------------------------------------------------ | ------- | ---------------------------------------------------------- |
| `SIBYL_AUTO_EXTRACT_ENTITIES`                    | `false` | Queue LLM entity extraction for prose-bearing memories     |
| `SIBYL_OPERATIONAL_NOTE_DISTILLATION_MAX_TOKENS` | `2048`  | Max output tokens per note distillation call (256-8192)    |
| `SIBYL_RAW_CAPTURE_CHANGEFEED_POLL_ENABLED`      | `true`  | Poll the raw-captures changefeed for incremental promotion |
| `SIBYL_RAW_CAPTURE_LIVE_QUERY_ENABLED`           | `false` | Use SurrealDB live queries for realtime promotion hints    |
| `SIBYL_RAW_CAPTURE_LIVE_QUERY_RETRY_SECONDS`     | `5.0`   | Delay before reconnecting the raw-capture live query       |

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

| Variable                | Default | Description                                          |
| ----------------------- | ------- | ---------------------------------------------------- |
| `SIBYL_WORKER_MAX_JOBS` | (auto)  | Override maximum concurrent background jobs (1-1024) |

When `SIBYL_WORKER_MAX_JOBS` is unset, the limit is derived from CPU count (2x cores, minimum 3)
capped by the effective content-client pool size.

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

# Reverse proxies allowed to report the client address: the ingress
# controller pods' own range, never the pod CIDR
SIBYL_FORWARDED_ALLOW_IPS=10.250.0.0/28

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
  SIBYL_FORWARDED_ALLOW_IPS: "10.250.0.0/28"
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
  SIBYL_SURREAL_PASSWORD: "<surreal-password>"
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

Sibyl is fully SurrealDB-backed, so `fully_surreal` always returns `True`. `Settings` has no
relational connection helpers.
