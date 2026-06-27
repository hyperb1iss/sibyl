// =============================================================================
// Application Configuration
// =============================================================================

export const APP_CONFIG = {
  VERSION: process.env.NEXT_PUBLIC_VERSION || '0.0.0',
  NAME: 'Sibyl',
  TAGLINE: 'Cross-agent memory',
  GITHUB_URL: 'https://github.com/hyperb1iss/sibyl',
  SPONSOR_URL: 'https://github.com/sponsors/hyperb1iss',
} as const;

// Timing constants (in milliseconds)
export const TIMING = {
  REFETCH_DELAY: 2000,
  HEALTH_CHECK_INTERVAL: 30000,
  STATS_REFRESH_INTERVAL: 30000,
  STALE_TIME: 60000, // 1 minute stale time for React Query
} as const;
