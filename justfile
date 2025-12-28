# Sibyl development tasks

default:
    @just --list

# Lint (ruff + pyright)
lint:
    uv run ruff check .
    uv run pyright

# Fix (ruff fix + format)
fix:
    uv run ruff check --fix .
    uv run ruff format .

# Fix all (including unsafe fixes)
fix-all:
    uv run ruff check --fix --unsafe-fixes .
    uv run ruff format .

# Format YAML and Markdown files with Prettier
fmt:
    npx prettier --write "**/*.{yaml,yml,md}"

# Run unit tests (no server required)
test *args:
    uv run pytest --ignore=tests/e2e {{args}}

# Run all tests (unit + e2e against dev server)
test-all *args:
    uv run pytest {{args}}

# =============================================================================
# E2E Testing (isolated by default - won't touch dev data)
# =============================================================================

# Run e2e tests with full isolation (spins up separate containers)
e2e *args:
    #!/usr/bin/env bash
    set -e
    trap 'echo "Cleaning up..."; kill $SERVER_PID 2>/dev/null; docker compose -f compose.e2e.yml down -v 2>/dev/null' EXIT

    # Start isolated services
    docker compose -f compose.e2e.yml up -d
    echo "Waiting for e2e services..."
    sleep 3

    # Set env for isolated services
    export SIBYL_FALKORDB_PORT=6381
    export SIBYL_FALKORDB_PASSWORD=e2e_test
    export SIBYL_POSTGRES_PORT=5434
    export SIBYL_POSTGRES_USER=sibyl_e2e
    export SIBYL_POSTGRES_PASSWORD=sibyl_e2e_password
    export SIBYL_POSTGRES_DB=sibyl_e2e

    # Run migrations
    echo "Running migrations..."
    uv run alembic upgrade head

    # Start server in background
    echo "Starting server..."
    uv run python -m sibyl.main &
    SERVER_PID=$!

    # Wait for server
    for i in {1..30}; do
        if curl -s http://localhost:3334/api/health > /dev/null 2>&1; then
            echo "✓ Server ready"
            break
        fi
        sleep 1
    done

    # Run tests
    echo "Running e2e tests..."
    uv run pytest tests/e2e -v {{args}}

# Run e2e tests against dev server (for debugging, uses your local data!)
e2e-dev *args:
    @echo "⚠️  Running against dev server - will use your local data!"
    uv run pytest tests/e2e {{args}}

# Start isolated e2e services (for manual testing)
e2e-up:
    docker compose -f compose.e2e.yml up -d
    @echo "Waiting for services..."
    @sleep 3
    @echo "✓ E2E services ready (FalkorDB:6381, PostgreSQL:5434)"

# Stop and remove e2e services
e2e-down:
    docker compose -f compose.e2e.yml down -v
    @echo "✓ E2E services stopped and cleaned up"

# Run the server
serve:
    uv run sibyl serve

# Dev mode with hot reload (backend only, run frontend separately with: cd web && pnpm dev)
dev:
    uv run uvicorn sibyl.main:create_dev_app --factory --reload --host localhost --port 3334

# Install sibyl CLI globally (available as `sibyl` command anywhere)
install:
    uv tool install . --force
    @echo "✓ sibyl installed globally at $(which sibyl)"

# Install sibyl CLI in editable mode (dev changes auto-apply)
install-editable:
    uv tool install . --editable --force
    @echo "✓ sibyl installed in editable mode at $(which sibyl)"

# Install sibyl skills globally (Claude + Codex)
install-skills:
    @echo "Installing Sibyl skills..."
    @mkdir -p ~/.claude/skills ~/.codex/skills
    @ln -sfn "$(pwd)/skills/sibyl-knowledge" ~/.claude/skills/sibyl-knowledge
    @ln -sfn "$(pwd)/skills/sibyl-project-manager" ~/.claude/skills/sibyl-project-manager
    @ln -sfn "$(pwd)/skills/sibyl-knowledge" ~/.codex/skills/sibyl-knowledge
    @ln -sfn "$(pwd)/skills/sibyl-project-manager" ~/.codex/skills/sibyl-project-manager
    @echo "✓ Installed to ~/.claude/skills and ~/.codex/skills"
