import { isPublicRoutePath } from '@/lib/public-routes';

const API_BASE = '/api';

type RefreshResult = 'refreshed' | 'terminal' | 'transient';

const AUTH_REFRESH_LOCK = 'sibyl-auth-refresh';

let isRefreshing = false;
let refreshPromise: Promise<RefreshResult> | null = null;
let refreshCooldownUntil = 0;
let logoutPromise: Promise<void> | null = null;

/**
 * Try to refresh the access token using the refresh token cookie.
 * Distinguishes expired sessions from temporary refresh failures so network
 * and service brownouts do not log the user out.
 */
async function tryRefreshToken(): Promise<RefreshResult> {
  const now = Date.now();
  if (now < refreshCooldownUntil) {
    return 'transient';
  }

  // If already refreshing, wait for that to complete
  if (isRefreshing && refreshPromise !== null) {
    return refreshPromise;
  }

  isRefreshing = true;
  refreshPromise = (async () => {
    try {
      const response = await fetch(`${API_BASE}/auth/refresh`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
      });
      if (response.ok) {
        refreshCooldownUntil = 0;
        return 'refreshed';
      }

      if (response.status === 401 || response.status === 403) {
        refreshCooldownUntil = 0;
        return 'terminal';
      }

      const retryAfter = response.headers.get('Retry-After');
      if (response.status === 429 && retryAfter) {
        const retryAfterSeconds = Number(retryAfter);
        if (Number.isFinite(retryAfterSeconds)) {
          refreshCooldownUntil = Date.now() + retryAfterSeconds * 1000;
          return 'transient';
        }

        const retryAt = Date.parse(retryAfter);
        if (!Number.isNaN(retryAt)) {
          refreshCooldownUntil = Math.max(Date.now() + 30_000, retryAt);
          return 'transient';
        }
      }

      // Default cooldown to avoid hammering refresh on repeated 401s across many requests.
      refreshCooldownUntil = Date.now() + (response.status === 429 ? 60_000 : 30_000);
      return 'transient';
    } catch {
      refreshCooldownUntil = Date.now() + 30_000;
      return 'transient';
    } finally {
      isRefreshing = false;
      refreshPromise = null;
    }
  })();

  return refreshPromise;
}

async function refreshAndRetry(
  makeRequest: () => Promise<Response>,
  unauthorizedResponse: Response
): Promise<Response> {
  const refreshResult = await tryRefreshToken();
  if (refreshResult === 'terminal') return unauthorizedResponse;
  if (refreshResult === 'transient') {
    throw new Error('Authentication refresh temporarily unavailable');
  }
  return makeRequest();
}

async function recoverUnauthorized(
  makeRequest: () => Promise<Response>,
  unauthorizedResponse: Response
): Promise<Response> {
  if (typeof navigator === 'undefined' || navigator.locks === undefined) {
    return refreshAndRetry(makeRequest, unauthorizedResponse);
  }

  const refreshResult = await navigator.locks.request(AUTH_REFRESH_LOCK, async () => {
    try {
      const currentSessionResponse = await fetch(`${API_BASE}/auth/me`, {
        credentials: 'include',
      });
      if (currentSessionResponse.ok) return 'refreshed';
      if (currentSessionResponse.status !== 401) return 'transient';
      return tryRefreshToken();
    } catch {
      return 'transient';
    }
  });

  if (refreshResult === 'terminal') return unauthorizedResponse;
  if (refreshResult === 'transient') {
    throw new Error('Authentication refresh temporarily unavailable');
  }
  return makeRequest();
}

async function bestEffortLogout(): Promise<void> {
  if (logoutPromise !== null) return logoutPromise;

  logoutPromise = (async () => {
    try {
      await fetch(`${API_BASE}/auth/logout`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        keepalive: true,
      });
    } catch {
      // Ignore network errors - we're already falling back to login.
    } finally {
      logoutPromise = null;
    }
  })();

  return logoutPromise;
}

/**
 * Redirect to login page with return URL.
 */
function redirectToLogin(): never {
  // Best-effort: clear cookies so middleware doesn't bounce `/login` back to `/`.
  void bestEffortLogout();

  const currentPath = window.location.pathname + window.location.search;
  window.location.href = `/login?next=${encodeURIComponent(currentPath)}`;
  // Return a promise that never resolves to prevent further execution
  return new Promise(() => {
    // Intentionally empty - blocks until page redirects
  }) as never;
}

function shouldRedirectAuthFailure(endpoint: string): boolean {
  return endpoint !== '/auth/refresh' && !isPublicRoutePath(window.location.pathname);
}

export async function fetchApi<T>(endpoint: string, options?: RequestInit): Promise<T> {
  const makeRequest = () =>
    fetch(`${API_BASE}${endpoint}`, {
      ...options,
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        ...options?.headers,
      },
    });

  const response = await makeRequest();

  if (!response.ok) {
    // Handle 401 - try to refresh token before redirecting to login
    if (response.status === 401 && typeof window !== 'undefined') {
      if (shouldRedirectAuthFailure(endpoint)) {
        const retryResponse = await recoverUnauthorized(makeRequest, response);

        if (retryResponse.ok) {
          if (retryResponse.status === 204) {
            return undefined as T;
          }
          return retryResponse.json();
        }

        if (retryResponse.status === 401) {
          return redirectToLogin();
        }

        const error = await retryResponse.text();
        throw new Error(error || `API error: ${retryResponse.status}`);
      }
    }

    const error = await response.text();
    throw new Error(error || `API error: ${response.status}`);
  }

  // Handle 204 No Content (e.g., DELETE responses)
  if (response.status === 204) {
    return undefined as T;
  }

  return response.json();
}

export async function fetchApiBlob(endpoint: string): Promise<Blob> {
  const makeRequest = () =>
    fetch(`${API_BASE}${endpoint}`, {
      credentials: 'include',
    });

  const response = await makeRequest();
  if (!response.ok) {
    if (response.status === 401 && typeof window !== 'undefined') {
      if (shouldRedirectAuthFailure(endpoint)) {
        const retryResponse = await recoverUnauthorized(makeRequest, response);
        if (retryResponse.ok) return retryResponse.blob();
        if (retryResponse.status === 401) return redirectToLogin();
        const error = await retryResponse.text();
        throw new Error(error || `API error: ${retryResponse.status}`);
      }
    }

    const error = await response.text();
    throw new Error(error || `API error: ${response.status}`);
  }

  return response.blob();
}
