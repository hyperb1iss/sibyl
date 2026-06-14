# ============================================================================
# Sibyl Frontend Dockerfile
# Multi-stage build for Next.js 16 with pnpm
# NOTE: Build from repo root context: docker build -f apps/web/Dockerfile .
# ============================================================================

# Stage 1: Dependencies
FROM node:24-alpine AS deps

RUN corepack enable && corepack prepare pnpm@11.5.2 --activate

WORKDIR /app

# Copy package files from monorepo root
COPY pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json ./apps/web/

# Install dependencies for web app only
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile --filter @sibyl/web


# Stage 2: Builder
FROM node:24-alpine AS builder

RUN corepack enable && corepack prepare pnpm@11.5.2 --activate

WORKDIR /app/apps/web

# Copy workspace structure needed for pnpm
COPY --from=deps /app/pnpm-lock.yaml /app/pnpm-workspace.yaml /app/
COPY --from=deps /app/node_modules /app/node_modules
COPY --from=deps /app/apps/web/node_modules ./node_modules

# Copy VERSION file for build-time version injection
COPY VERSION /app/

# Copy app source
COPY apps/web .

# Set production env for build
# SIBYL_BACKEND_URL uses Docker service name for server-side rewrites
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    SIBYL_BACKEND_URL=http://api:3334 \
    SIBYL_NEXT_BUILD_CPUS=2

# Turbopack can exhaust worker threads in constrained cluster builders.
RUN pnpm exec next build --webpack


# Stage 3: Runtime
FROM node:24-alpine AS runner

WORKDIR /app/apps/web

# Create non-root user
RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# Set production env
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3337 \
    HOSTNAME=0.0.0.0

# Copy standalone output (preserves monorepo structure)
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/standalone /app
COPY --from=builder /app/apps/web/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/static ./.next/static

# Next.js needs writable .next tree at runtime (image cache, etc.)
RUN mkdir -p .next/cache && chown -R nextjs:nodejs .next

# Switch to non-root user
USER nextjs

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:3337/ || exit 1

# Expose port
EXPOSE 3337

# Run Next.js
CMD ["node", "server.js"]
