import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';

const apiServer = vi.hoisted(() => ({
  fetchEntities: vi.fn(),
  fetchStats: vi.fn(),
}));

const entitiesContent = vi.hoisted(() => vi.fn(() => <div data-testid="entities-content" />));

vi.mock('@/lib/api-server', () => apiServer);
vi.mock('./entities-content', () => ({
  EntitiesContent: entitiesContent,
}));

import EntitiesPage from './page';

describe('EntitiesPage', () => {
  it('leaves failed server data unknown for client-side recovery', async () => {
    apiServer.fetchEntities.mockRejectedValue(new Error('backend down'));
    apiServer.fetchStats.mockRejectedValue(new Error('backend down'));

    render(
      await EntitiesPage({
        searchParams: Promise.resolve({}),
      })
    );

    expect(screen.getByTestId('entities-content')).toBeInTheDocument();
    expect(entitiesContent).toHaveBeenCalledWith(
      expect.objectContaining({
        initialEntities: undefined,
        initialStats: undefined,
        search: '',
        page: 1,
        sortBy: 'updated_at',
        sortOrder: 'desc',
      }),
      undefined
    );
  });
});
