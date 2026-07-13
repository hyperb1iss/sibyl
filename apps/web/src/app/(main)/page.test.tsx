import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';

const apiServer = vi.hoisted(() => ({
  fetchStats: vi.fn(),
}));

const dashboardContent = vi.hoisted(() => vi.fn(() => <div data-testid="dashboard-content" />));

vi.mock('@/lib/api-server', () => apiServer);
vi.mock('./dashboard-content', () => ({
  DashboardContent: dashboardContent,
}));

import DashboardPage from './page';

describe('DashboardPage', () => {
  it('leaves failed server stats unknown for client-side recovery', async () => {
    apiServer.fetchStats.mockRejectedValue(new Error('stale access token'));

    render(await DashboardPage());

    expect(screen.getByTestId('dashboard-content')).toBeInTheDocument();
    expect(dashboardContent).toHaveBeenCalledWith({ initialStats: undefined }, undefined);
  });
});
