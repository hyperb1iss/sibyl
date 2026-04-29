import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('server-only', () => ({}));
vi.mock('next-dynenv', () => ({
  serverOnly: (_key: string, fallback: string) => fallback,
}));
vi.mock('next/headers', () => ({
  cookies: vi.fn(async () => ({
    toString: (): string => 'sibyl_access_token=test-token',
  })),
}));

describe('api-server', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.restoreAllMocks();
  });

  it('applies a timeout signal to server fetches', async () => {
    const timeoutSignal = new AbortController().signal;
    const timeoutSpy = vi.spyOn(AbortSignal, 'timeout').mockReturnValue(timeoutSignal);
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({ entity_counts: {}, total_entities: 0 }),
    }));
    vi.stubGlobal('fetch', fetchMock);

    const apiServer = await import('./api-server');
    await apiServer.fetchStats();

    expect(timeoutSpy).toHaveBeenCalledWith(5000);
    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:3334/api/admin/stats',
      expect.objectContaining({
        signal: timeoutSignal,
        headers: expect.objectContaining({
          cookie: 'sibyl_access_token=test-token',
        }),
      })
    );
  });

  it('wraps timeout failures with a readable error', async () => {
    vi.spyOn(AbortSignal, 'timeout').mockReturnValue(new AbortController().signal);
    const fetchMock = vi.fn(async () => {
      throw Object.assign(new Error('fetch aborted'), { name: 'TimeoutError' });
    });
    vi.stubGlobal('fetch', fetchMock);

    const apiServer = await import('./api-server');

    await expect(apiServer.fetchStats()).rejects.toThrow(
      'API request timed out after 5000ms: /admin/stats'
    );
  });
});
