# ============================================================================
# Sibyl Backend Dockerfile
# Multi-stage build for Python 3.13+ with uv package manager
# ============================================================================

# Stage 1: Builder
FROM python:3.13-slim-bookworm AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies first (cached unless requirements change)
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Copy source and install project
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# Stage 2: Runtime
FROM python:3.13-slim-bookworm AS runtime

# Create non-root user
RUN groupadd --gid 1000 sibyl && \
    useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home sibyl

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder --chown=sibyl:sibyl /app/.venv /app/.venv
COPY --from=builder --chown=sibyl:sibyl /app/alembic /app/alembic
COPY --from=builder --chown=sibyl:sibyl /app/alembic.ini /app/alembic.ini

# Set environment
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
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
CMD ["python", "-m", "sibyl.main", "serve"]
