# ============================================================================
# Sibyl Frontend Dockerfile
# Multi-stage build for Next.js 16 with pnpm
# NOTE: Build from repo root context: docker build -f apps/web/Dockerfile .
# ============================================================================

# Stage 1: Dependencies
FROM node:22-alpine AS deps

RUN corepack enable pnpm

WORKDIR /app

# Copy package files from monorepo root
COPY pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json ./apps/web/

# Install dependencies for web app only
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile --filter @sibyl/web


# Stage 2: Builder
FROM node:22-alpine AS builder

RUN corepack enable pnpm

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
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

# Build the app
RUN pnpm build


# Stage 3: Runtime
FROM node:22-alpine AS runner

WORKDIR /app

# Create non-root user
RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# Set production env
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3337 \
    HOSTNAME=0.0.0.0

# Copy standalone output (most efficient)
COPY --from=builder /app/apps/web/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/static ./.next/static

# Switch to non-root user
USER nextjs

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:3337/ || exit 1

# Expose port
EXPOSE 3337

# Run Next.js
CMD ["node", "server.js"]
