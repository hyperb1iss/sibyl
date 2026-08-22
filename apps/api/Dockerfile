# ============================================================================
# Sibyld Backend Dockerfile
# Multi-stage build for Python 3.13+ with uv package manager
# ============================================================================

# Stage 1: Builder
FROM python:3.13.15-slim-bookworm AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/

WORKDIR /app

# Copy workspace config for dependency resolution
COPY pyproject.toml uv.lock VERSION ./
COPY packages/python/sibyl-core/pyproject.toml packages/python/sibyl-core/README.md packages/python/sibyl-core/
COPY packages/python/sibyl-core/src packages/python/sibyl-core/src

# Copy API package
COPY apps/api/pyproject.toml apps/api/README.md apps/api/
COPY apps/api/src apps/api/src
COPY README.md ./

# Install from workspace (--no-editable for proper wheel builds in Docker)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package sibyld


# Stage 2: Runtime
FROM python:3.13.15-slim-bookworm AS runtime

# Build provenance. Without these the running server cannot name itself:
# _read_version() falls back to "0.0.0" and get_runtime_provenance()
# reports "unknown", because the runtime stage has no VERSION file and no
# git. Both are stamped on every response for client-drift detection, so
# they have to survive into this stage.
ARG SIBYL_VERSION=0.0.0
ARG SIBYL_GIT_COMMIT=unknown
ARG SIBYL_GIT_DIRTY=false

# Create non-root user with a high service UID to avoid host user collisions.
RUN groupadd --gid 10001 sibyl && \
    useradd --uid 10001 --gid 10001 --shell /bin/bash --create-home sibyl

# The base image ships pip only to bootstrap installs. Nothing here runs it:
# the application lives in /app/.venv, which uv builds without pip. What pip
# does contribute is its vendored dependency set, declared in
# pip/_vendor/vendor.txt and read by scanners as image contents, which is how
# msgpack and setuptools advisories reached a release that depends on
# neither. The final import check fails the build if a future base image
# reinstates pip somewhere this does not reach.
RUN site_packages="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" && \
    rm -rf "${site_packages}/pip" "${site_packages}"/pip-*.dist-info && \
    rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.* && \
    ! python -c "import pip" 2>/dev/null

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder --chown=sibyl:sibyl /app/.venv /app/.venv

# Belt and braces: the env var is authoritative, the file is the fallback
# _read_version() looks for at /app/VERSION.
COPY --chown=sibyl:sibyl VERSION /app/VERSION

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
    SIBYL_SERVER_PORT=3334 \
    # Build provenance, promoted from build args so the server can name
    # itself to clients checking for drift.
    SIBYL_VERSION=${SIBYL_VERSION} \
    SIBYL_GIT_COMMIT=${SIBYL_GIT_COMMIT} \
    SIBYL_GIT_DIRTY=${SIBYL_GIT_DIRTY}

# Switch to non-root user
USER sibyl

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:3334/api/health')" || exit 1

# Expose MCP + API port
EXPOSE 3334

# Run the server
CMD ["sibyld", "serve"]
