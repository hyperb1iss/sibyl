# ============================================================================
# Sibyl Runner Dockerfile
# Distributed agent execution daemon for cloud deployment
# ============================================================================

# Stage 1: Builder
FROM python:3.13-slim-bookworm AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy workspace config for dependency resolution
COPY pyproject.toml uv.lock VERSION ./
COPY packages/python/sibyl-core/pyproject.toml packages/python/sibyl-core/README.md packages/python/sibyl-core/
COPY packages/python/sibyl-core/src packages/python/sibyl-core/src

# Copy runner package
COPY apps/runner/pyproject.toml apps/runner/README.md apps/runner/
COPY apps/runner/src apps/runner/src
COPY README.md ./

# Install from workspace (--no-editable for proper wheel builds in Docker)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package sibyl-runner


# Stage 2: Runtime
FROM python:3.13-slim-bookworm AS runtime

# Install runtime dependencies
# - git: For worktree management
# - nodejs: For Claude Code / MCP tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (for Claude Code)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd --gid 1000 sibyl && \
    useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home sibyl

# Create worktree directory
RUN mkdir -p /var/sibyl/worktrees && \
    chown -R sibyl:sibyl /var/sibyl

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder --chown=sibyl:sibyl /app/.venv /app/.venv

# Set environment
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Default worktree location
    SIBYL_WORKTREE_BASE=/var/sibyl/worktrees \
    # Default config location
    SIBYL_CONFIG_DIR=/var/sibyl/config

# Create config directory
RUN mkdir -p /var/sibyl/config && \
    chown -R sibyl:sibyl /var/sibyl

# Switch to non-root user
USER sibyl

# Health check - verify the runner daemon is responsive
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD sibyl-runner status || exit 1

# Volumes for persistence
VOLUME ["/var/sibyl/worktrees", "/var/sibyl/config"]

# Run the daemon
# Note: Runner must be registered first (sibyl-runner register)
# Config is expected at /var/sibyl/config/runner.yaml
CMD ["sibyl-runner", "run"]
