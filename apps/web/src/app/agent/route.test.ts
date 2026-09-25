import { afterEach, describe, expect, it, vi } from 'vitest';
import { GET } from './route';

describe('GET /agent', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it('relays the agent setup markdown from the API', async () => {
    vi.stubEnv('SIBYL_API_URL', 'http://backend.internal:3334/api');
    const fetchMock = vi.fn().mockResolvedValue(
      new Response('# Set up Sibyl on this machine\n', {
        status: 200,
        headers: { 'content-type': 'text/markdown; charset=utf-8' },
      })
    );
    vi.stubGlobal('fetch', fetchMock);

    const response = await GET();

    expect(fetchMock.mock.calls[0][0]).toBe('http://backend.internal:3334/api/setup/agent.md');
    expect(response.status).toBe(200);
    expect(response.headers.get('content-type')).toBe('text/markdown; charset=utf-8');
    expect(await response.text()).toBe('# Set up Sibyl on this machine\n');
  });

  it('answers 502 in markdown when the API is unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('fetch failed')));

    const response = await GET();

    expect(response.status).toBe(502);
    expect(response.headers.get('content-type')).toBe('text/markdown; charset=utf-8');
    expect(await response.text()).toContain('did not answer');
  });
});
