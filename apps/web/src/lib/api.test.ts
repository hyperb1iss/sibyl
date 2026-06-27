import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { isSetupAlreadyInitializedError } from './api';

describe('isSetupAlreadyInitializedError', () => {
  it('matches structured setup initialization errors', () => {
    const error = new Error(
      '{"detail":{"code":"setup_already_initialized","message":"Setup is complete."}}'
    );

    expect(isSetupAlreadyInitializedError(error)).toBe(true);
  });

  it('matches legacy setup complete messages', () => {
    expect(isSetupAlreadyInitializedError(new Error('Setup is complete.'))).toBe(true);
  });

  it('ignores unrelated errors and non-errors', () => {
    expect(isSetupAlreadyInitializedError(new Error('Admin or owner role required'))).toBe(false);
    expect(isSetupAlreadyInitializedError('setup_already_initialized')).toBe(false);
  });
});

// -----------------------------------------------------------------------------
// Fetch mocking harness
//
// fetchApi, tryRefreshToken, and the refresh cooldown live as module-private
// state, so the auth state machine can only be exercised through the exported
// `api` surface. Each test re-imports the module (vi.resetModules) to start from
// a clean refresh/cooldown slate, then drives a scripted fetch queue.
// -----------------------------------------------------------------------------

type MockResponse = {
  ok: boolean;
  status: number;
  json?: () => Promise<unknown>;
  text?: () => Promise<string>;
  blob?: () => Promise<Blob>;
  headers?: Headers;
};

function jsonResponse(body: unknown, status = 200): MockResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: new Headers(),
  };
}

function errorResponse(status: number, body = ''): MockResponse {
  return {
    ok: false,
    status,
    json: async () => (body ? JSON.parse(body) : {}),
    text: async () => body,
    headers: new Headers(),
  };
}

/**
 * Builds a fetch mock that returns the next queued response per matched URL
 * suffix, so a 401 → refresh → retry sequence can be scripted in order.
 */
function scriptFetch(steps: Array<{ match: string; response: MockResponse }>) {
  const queue = [...steps];
  const calls: Array<{ url: string; init?: RequestInit }> = [];

  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const idx = queue.findIndex(step => url.includes(step.match));
    if (idx === -1) {
      throw new Error(`unexpected fetch: ${url}`);
    }
    const [step] = queue.splice(idx, 1);
    return step.response as unknown as Response;
  });

  vi.stubGlobal('fetch', fetchMock);
  return { fetchMock, calls };
}

/**
 * Replaces window.location with a plain object whose `href` assignment is
 * observable. redirectToLogin() sets href and returns a never-resolving
 * promise, so tests assert the side effect rather than awaiting the caller.
 */
function stubLocation(pathname = '/dashboard', search = '') {
  const setHref = vi.fn();
  const location = {
    pathname,
    search,
    get href() {
      return '';
    },
    set href(value: string) {
      setHref(value);
    },
  };
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: location,
  });
  return setHref;
}

/** Flush the microtask queue so scripted fetches/redirect side effects settle. */
async function flush() {
  for (let i = 0; i < 10; i += 1) {
    await Promise.resolve();
  }
}

describe('fetchApi auth state machine', () => {
  beforeEach(() => {
    vi.resetModules();
    stubLocation('/dashboard');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('refreshes and retries once on a 401, returning the retried payload', async () => {
    const me = { user: { id: 'u1', email: 'a@b.co' } };
    const { fetchMock } = scriptFetch([
      { match: '/auth/me', response: errorResponse(401) },
      { match: '/auth/refresh', response: jsonResponse({}, 200) },
      { match: '/auth/me', response: jsonResponse(me, 200) },
    ]);

    const { api } = await import('./api');
    const result = await api.auth.me();

    expect(result).toEqual(me);
    // original (401) + refresh + retry (200)
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const refreshCall = fetchMock.mock.calls.find(([url]) => String(url).includes('/auth/refresh'));
    expect(refreshCall?.[1]).toMatchObject({ method: 'POST', credentials: 'include' });
  });

  it('redirects to login when the retry after refresh still 401s', async () => {
    const setHref = stubLocation('/sources');
    scriptFetch([
      { match: '/auth/me', response: errorResponse(401) },
      { match: '/auth/refresh', response: errorResponse(401) },
      { match: '/auth/me', response: errorResponse(401) },
      // redirectToLogin fires a best-effort logout
      { match: '/auth/logout', response: jsonResponse({}, 200) },
    ]);

    const { api } = await import('./api');
    // redirect path returns a promise that never resolves; assert the side effect.
    void api.auth.me();
    await flush();

    expect(setHref).toHaveBeenCalledWith('/login?next=%2Fsources');
  });

  it('does not attempt refresh on the login page', async () => {
    stubLocation('/login');
    const { fetchMock } = scriptFetch([
      { match: '/auth/me', response: errorResponse(401, 'nope') },
    ]);

    const { api } = await import('./api');
    await expect(api.auth.me()).rejects.toThrow('nope');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does not redirect public password reset pages on auth probe failure', async () => {
    const setHref = stubLocation('/reset-password', '?token=reset-token');
    const { fetchMock } = scriptFetch([{ match: '/auth/me', response: errorResponse(401) }]);

    const { api } = await import('./api');
    await expect(api.auth.me()).rejects.toThrow('API error: 401');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(setHref).not.toHaveBeenCalled();
  });

  it('shares one refresh across concurrent 401s instead of refreshing per request', async () => {
    const { fetchMock } = scriptFetch([
      { match: '/auth/me', response: errorResponse(401) },
      { match: '/users/me/preferences', response: errorResponse(401) },
      { match: '/auth/refresh', response: jsonResponse({}, 200) },
      { match: '/auth/me', response: jsonResponse({ user: { id: 'u1' } }, 200) },
      { match: '/users/me/preferences', response: jsonResponse({ preferences: {} }, 200) },
    ]);

    const { api } = await import('./api');
    await Promise.all([api.auth.me(), api.preferences.get()]);

    const refreshCalls = fetchMock.mock.calls.filter(([url]) =>
      String(url).includes('/auth/refresh')
    );
    expect(refreshCalls).toHaveLength(1);
  });

  it('honors a Retry-After cooldown and skips refresh on the next 401', async () => {
    const cooldownHeaders = new Headers({ 'Retry-After': '120' });
    scriptFetch([
      { match: '/auth/me', response: errorResponse(401) },
      {
        match: '/auth/refresh',
        response: { ...errorResponse(429), headers: cooldownHeaders },
      },
      // retry still 401 → redirect (best-effort logout fires)
      { match: '/auth/me', response: errorResponse(401) },
      { match: '/auth/logout', response: jsonResponse({}, 200) },
    ]);

    const setHref = stubLocation('/dashboard');
    const { api } = await import('./api');
    void api.auth.me();
    await flush();
    expect(setHref).toHaveBeenCalled();

    // Second 401 within cooldown: refresh must be skipped, so only the
    // original request fires before redirecting.
    const { fetchMock } = scriptFetch([
      { match: '/users/me/preferences', response: errorResponse(401) },
      { match: '/users/me/preferences', response: errorResponse(401) },
      { match: '/auth/logout', response: jsonResponse({}, 200) },
    ]);
    void api.preferences.get();
    await flush();

    const refreshCalls = fetchMock.mock.calls.filter(([url]) =>
      String(url).includes('/auth/refresh')
    );
    expect(refreshCalls).toHaveLength(0);
  });

  it('returns undefined for 204 responses without parsing a body', async () => {
    scriptFetch([
      {
        match: '/auth/logout',
        response: {
          ok: true,
          status: 204,
          json: () => {
            throw new Error('204 must not be parsed');
          },
          headers: new Headers(),
        },
      },
    ]);

    const { api } = await import('./api');
    await expect(api.auth.logout()).resolves.toBeUndefined();
  });

  it('posts password reset request and confirmation payloads', async () => {
    const { fetchMock } = scriptFetch([
      {
        match: '/users/password/reset',
        response: jsonResponse({ message: 'If an account exists, a reset email has been sent.' }),
      },
      {
        match: '/users/password/reset/confirm',
        response: { ok: true, status: 204, headers: new Headers() },
      },
    ]);

    const { api } = await import('./api');
    await expect(
      api.security.requestPasswordReset({ email: 'stef@hyperbliss.tech' })
    ).resolves.toEqual({
      message: 'If an account exists, a reset email has been sent.',
    });
    await expect(
      api.security.confirmPasswordReset({
        token: 'reset-token',
        new_password: 'new-password',
      })
    ).resolves.toEqual({ success: true });

    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      email: 'stef@hyperbliss.tech',
    });
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      token: 'reset-token',
      new_password: 'new-password',
    });
  });
});

describe('backend record normalizers', () => {
  beforeEach(() => {
    vi.resetModules();
    stubLocation('/settings/security');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('normalizes API key records, including the prefix fallback and missing arrays', async () => {
    scriptFetch([
      {
        match: '/auth/api-keys',
        response: jsonResponse({
          keys: [
            {
              id: 'k1',
              name: 'CI token',
              key_prefix: 'sk_ci',
              scopes: ['mcp'],
              project_ids: ['p1'],
              last_used_at: '2026-05-01T00:00:00Z',
            },
            { id: 'k2', name: 'bare' },
          ],
        }),
      },
    ]);

    const { api } = await import('./api');
    const { api_keys } = await api.security.apiKeys.list();

    expect(api_keys[0]).toEqual({
      id: 'k1',
      name: 'CI token',
      prefix: 'sk_ci',
      scopes: ['mcp'],
      project_ids: ['p1'],
      memory_space_ids: [],
      last_used_at: '2026-05-01T00:00:00Z',
      expires_at: null,
      created_at: null,
    });
    // missing optional fields collapse to safe defaults
    expect(api_keys[1]).toMatchObject({
      id: 'k2',
      prefix: '',
      scopes: [],
      project_ids: [],
      memory_space_ids: [],
    });
  });

  it('returns the one-time key alongside the normalized record on create', async () => {
    scriptFetch([
      {
        match: '/auth/api-keys',
        response: jsonResponse({
          id: 'k9',
          name: 'fresh',
          prefix: 'sk_fresh',
          api_key: 'sk_fresh_secret_value',
        }),
      },
    ]);

    const { api } = await import('./api');
    const result = await api.security.apiKeys.create({ name: 'fresh' });

    expect(result.key).toBe('sk_fresh_secret_value');
    expect(result.api_key).toMatchObject({ id: 'k9', name: 'fresh', prefix: 'sk_fresh' });
    // the raw one-time secret must not bleed into the normalized record shape
    expect(result.api_key).not.toHaveProperty('api_key');
  });

  it('passes session records through under the sessions envelope', async () => {
    const sessions = [
      {
        id: 's1',
        user_agent: 'Firefox',
        ip_address: '10.0.0.1',
        created_at: '2026-05-01T00:00:00Z',
        expires_at: '2026-06-01T00:00:00Z',
        last_used_at: null,
        is_current: true,
      },
    ];
    scriptFetch([{ match: '/users/me/sessions', response: jsonResponse(sessions) }]);

    const { api } = await import('./api');
    await expect(api.security.sessions.list()).resolves.toEqual({ sessions });
  });

  it('reshapes crawl sources into the source-summary list payload', async () => {
    scriptFetch([
      {
        match: '/sources',
        response: jsonResponse({
          total: 1,
          sources: [
            {
              id: 'src1',
              name: 'Docs',
              url: 'https://docs.example.com',
              source_type: 'website',
              description: 'API docs',
              crawl_depth: 3,
              crawl_status: 'completed',
              document_count: 12,
              chunk_count: 40,
              last_crawled_at: '2026-05-10T00:00:00Z',
              last_error: null,
              created_at: '2026-05-01T00:00:00Z',
              include_patterns: ['/docs/**'],
              exclude_patterns: ['/blog/**'],
            },
          ],
        }),
      },
    ]);

    const { api } = await import('./api');
    const result = await api.sources.list();

    expect(result.mode).toBe('list');
    expect(result.total).toBe(1);
    const [entity] = result.entities;
    expect(entity).toMatchObject({
      id: 'src1',
      type: 'source',
      name: 'Docs',
      description: 'API docs',
      updated_at: '2026-05-10T00:00:00Z',
    });
    expect(entity.metadata).toMatchObject({
      url: 'https://docs.example.com',
      source_type: 'website',
      crawl_status: 'completed',
      document_count: 12,
      last_crawled: '2026-05-10T00:00:00Z',
      crawl_depth: 3,
      crawl_patterns: ['/docs/**'],
      exclude_patterns: ['/blog/**'],
    });
  });

  it('falls back to created_at when a source has never been crawled', async () => {
    scriptFetch([
      {
        match: '/sources',
        response: jsonResponse({
          total: 1,
          sources: [
            {
              id: 'src2',
              name: 'New',
              url: 'https://new.example.com',
              source_type: 'website',
              description: null,
              crawl_depth: 2,
              crawl_status: 'pending',
              document_count: 0,
              chunk_count: 0,
              last_crawled_at: null,
              last_error: null,
              created_at: '2026-05-20T00:00:00Z',
              include_patterns: [],
              exclude_patterns: [],
            },
          ],
        }),
      },
    ]);

    const { api } = await import('./api');
    const result = await api.sources.list();
    const [entity] = result.entities;

    expect(entity.updated_at).toBe('2026-05-20T00:00:00Z');
    expect(entity.description).toBe('');
    expect(entity.metadata.last_crawled).toBeUndefined();
  });
});

describe('query param builders', () => {
  beforeEach(() => {
    vi.resetModules();
    stubLocation('/dashboard');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('serializes admin audit params and omits unset fields', async () => {
    const { fetchMock } = scriptFetch([
      { match: '/admin/audit', response: jsonResponse({ events: [], total: 0, limit: 50 }) },
    ]);

    const { api } = await import('./api');
    await api.admin.audit.list({ action: 'login', limit: 25, offset: 0 });

    const [url] = fetchMock.mock.calls[0];
    const query = new URL(String(url), 'http://localhost').searchParams;
    expect(query.get('action')).toBe('login');
    expect(query.get('limit')).toBe('25');
    // offset 0 is falsy and intentionally dropped; unset fields are absent
    expect(query.has('offset')).toBe(false);
    expect(query.has('user_id')).toBe(false);
    expect(query.has('resource')).toBe(false);
  });

  it('appends each project id and sort params on the entity list query', async () => {
    const { fetchMock } = scriptFetch([
      {
        match: '/entities',
        response: jsonResponse({
          entities: [],
          total: 0,
          page: 1,
          page_size: 20,
          has_more: false,
        }),
      },
    ]);

    const { api } = await import('./api');
    await api.entities.list({
      entity_type: 'task',
      project_ids: ['p1', 'p2'],
      sort_by: 'created_at',
      sort_order: 'desc',
      page: 2,
    });

    const [url] = fetchMock.mock.calls[0];
    const query = new URL(String(url), 'http://localhost').searchParams;
    expect(query.getAll('project_ids')).toEqual(['p1', 'p2']);
    expect(query.get('entity_type')).toBe('task');
    expect(query.get('sort_by')).toBe('created_at');
    expect(query.get('sort_order')).toBe('desc');
    expect(query.get('page')).toBe('2');
  });
});
