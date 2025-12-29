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

## Running Tests

```bash
# All e2e tests
moon run e2e:test

# API & CLI tests only
moon run e2e:test-api

# Browser tests only
moon run e2e:test-browser
```

## Test Categories

| Marker    | Description                          |
| --------- | ------------------------------------ |
| `api`     | REST API endpoint tests              |
| `cli`     | CLI command tests (via subprocess)   |
| `browser` | Browser automation tests (Playwright)|
| `slow`    | Long-running tests                   |

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
