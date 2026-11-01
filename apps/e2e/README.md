# Sibyl E2E Tests

End-to-end tests for the complete Sibyl system.

## Prerequisites

E2E tests require running services:

```bash
# Start all services
moon run dev

# Or manually:
moon run api:serve    # API server on :3334
moon run api:worker   # Background worker
moon run web:dev      # Web UI on :3337 (for browser tests)
```

For an isolated SurrealDB data service instead of the shared dev database:

```bash
moon run e2e-up
SIBYL_SURREAL_URL=ws://localhost:8011/rpc moon run api:serve
```

## Running Tests

```bash
# All e2e tests
moon run e2e:test

# API & CLI tests only
moon run e2e:test-api

# Browser tests only
moon run e2e:test-browser

# Live multi-user performance suite
moon run e2e:test-perf
```

Moon runs the CLI E2E suite against the repo checkout by setting
`SIBYL_E2E_CLI_COMMAND="uv run --project ../cli sibyl"`, so local test runs do
not depend on a separately installed global `sibyl`.

## Sign-In Budget

The login route allows five attempts a minute per client address, and a CI run signs in from one
runner. Each pytest process signs in once through the API with `SIBYL_E2E_EMAIL` and
`SIBYL_E2E_PASSWORD`, or not at all when `SIBYL_E2E_ACCESS_TOKEN` hands it a session for that
account; CI hands over the one the baseline seed opened. The browser run also signs in once through
the real login page either way. With `SIBYL_E2E_API_LOG` pointing at the API server's log, the
browser run fails when any client address signed in five or more times over the whole job. The auth
lifecycle replay signs in from its own forwarded address, as a separate user on another machine
would.

## Performance Suite

`moon run e2e:test-perf` runs an opt-in live load suite against the configured API. It does not
start services; use an already running stack, preferably backed by the isolated E2E SurrealDB
service when you do not want synthetic users and memory captures in the shared dev database.

The suite signs up isolated perf users, runs concurrent loops across raw memory writes, graph
entity writes, raw recall, search, and context-pack reads, then writes a JSON report under
`.moon/cache/perf-results/` by default.

Useful knobs:

```bash
SIBYL_PERF_USERS=8 \
SIBYL_PERF_ITERATIONS=10 \
SIBYL_PERF_MAX_ERROR_RATE=0 \
SIBYL_PERF_MAX_P95_MS=2500 \
moon run e2e:test-perf
```

`SIBYL_PERF_MAX_P95_MS=0` or an unset value disables the latency gate while still recording the
measurement. The error-rate gate defaults to `0`.

## Test Categories

| Marker    | Description                           |
| --------- | ------------------------------------- |
| `api`     | REST API endpoint tests               |
| `cli`     | CLI command tests (via subprocess)    |
| `browser` | Browser automation tests (Playwright) |
| `perf`    | Live multi-user performance tests     |
| `slow`    | Long-running tests                    |

## Browser Tests Setup

Browser tests use Playwright. Install browsers first:

```bash
moon run e2e:playwright-install
```

## Structure

```
tests/
├── conftest.py       # Shared fixtures (auth, clients, health checks)
├── api/              # API endpoint tests
├── cli/              # CLI command tests
└── browser/          # Browser automation tests
```
