// =============================================================================
// Application Configuration
// =============================================================================

export const APP_CONFIG = {
  VERSION: process.env.NEXT_PUBLIC_VERSION || '0.0.0',
  NAME: 'Sibyl',
  TAGLINE: 'Knowledge Oracle',
} as const;

// Timing constants (in milliseconds)
export const TIMING = {
  REFETCH_DELAY: 2000,
  HEALTH_CHECK_INTERVAL: 30000,
  STATS_REFRESH_INTERVAL: 30000,
  STALE_TIME: 60000, // 1 minute stale time for React Query
  AGENT_POLL_INTERVAL: 5000, // Poll agents every 5 seconds for status updates
} as const;
