# ============================================================================
# Sibyl Frontend Dockerfile
# Multi-stage build for Next.js 16 with pnpm
# ============================================================================

# Stage 1: Dependencies
FROM node:22-alpine AS deps

RUN corepack enable pnpm

WORKDIR /app

# Copy package files
COPY package.json pnpm-lock.yaml ./

# Install dependencies
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile


# Stage 2: Builder
FROM node:22-alpine AS builder

RUN corepack enable pnpm

WORKDIR /app

# Copy deps from previous stage
COPY --from=deps /app/node_modules ./node_modules
COPY . .

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
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

# Switch to non-root user
USER nextjs

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:3337/ || exit 1

# Expose port
EXPOSE 3337

# Run Next.js
CMD ["node", "server.js"]
