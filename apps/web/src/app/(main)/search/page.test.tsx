import { beforeEach, describe, expect, it, vi } from 'vitest';
import SearchPage from './page';

const api = vi.hoisted(() => ({
  fetchSearchResults: vi.fn(),
  fetchStats: vi.fn(async () => undefined),
}));
vi.mock('@/lib/api-server', () => api);
vi.mock('./search-content', () => ({ SearchContent: () => null }));

describe('SearchPage project boundary', () => {
  beforeEach(() => vi.clearAllMocks());
  it('does not prefetch memory before the browser resolves its project selection', async () => {
    await SearchPage({ searchParams: Promise.resolve({ q: 'telescope', mode: 'all' }) });
    expect(api.fetchSearchResults).not.toHaveBeenCalled();
    expect(api.fetchStats).toHaveBeenCalledOnce();
  });
});
