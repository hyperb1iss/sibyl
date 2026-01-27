# ============================================================================
# Sibyld Backend Dockerfile
# Multi-stage build for Python 3.13+ with uv package manager
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

# Copy API package
COPY apps/api/pyproject.toml apps/api/README.md apps/api/
COPY apps/api/src apps/api/src
COPY apps/api/alembic apps/api/alembic
COPY apps/api/alembic.ini apps/api/
COPY README.md ./

# Install from workspace (--no-editable for proper wheel builds in Docker)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package sibyld


# Stage 2: Runtime
FROM python:3.13-slim-bookworm AS runtime

# Create non-root user
RUN groupadd --gid 1000 sibyl && \
    useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home sibyl

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder --chown=sibyl:sibyl /app/.venv /app/.venv
COPY --from=builder --chown=sibyl:sibyl /app/apps/api/alembic /app/alembic
COPY --from=builder --chown=sibyl:sibyl /app/apps/api/alembic.ini /app/alembic.ini

# Install Playwright for crawl4ai (chromium headless)
# Manual minimal deps instead of --with-deps (which pulls ~600MB of X11/GUI stuff)
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/* \
    && /app/.venv/bin/playwright install chromium \
    && chown -R sibyl:sibyl /app/.playwright

# Set environment
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # crawl4ai needs a writable directory
    CRAWL4_AI_BASE_DIRECTORY=/tmp \
    # Sibyl defaults (override in compose/k8s)
    SIBYL_SERVER_HOST=0.0.0.0 \
    SIBYL_SERVER_PORT=3334

# Switch to non-root user
USER sibyl

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:3334/api/health')" || exit 1

# Expose MCP + API port
EXPOSE 3334

# Run the server
CMD ["sibyld", "serve"]
