import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { OrgMetricsResponse, StatsResponse } from '@/lib/api';
import { render, screen } from '@/test/utils';

const hooks = vi.hoisted(() => ({
  useCreateEntity: vi.fn(),
  useHealth: vi.fn(),
  useOrgMetrics: vi.fn(),
  useProjects: vi.fn(),
  useStats: vi.fn(),
  useTasks: vi.fn(),
}));

vi.mock('@/lib/hooks', () => hooks);

vi.mock('@/components/dashboard', () => ({
  CaptureMemoryDialog: () => <div data-testid="capture-memory-dialog" />,
  WelcomeBanner: () => <div data-testid="welcome-banner" />,
}));

vi.mock('@/components/metrics/charts', () => ({
  VelocityLineChart: () => <div data-testid="velocity-chart" />,
}));

import { DashboardContent } from './dashboard-content';

describe('DashboardContent', () => {
  const initialStats: StatsResponse = {
    entity_counts: {
      pattern: 3,
      task: 10,
    },
    total_entities: 13,
  };

  const orgMetrics: OrgMetricsResponse = {
    total_projects: 2,
    total_tasks: 10,
    status_distribution: {
      backlog: 0,
      todo: 2,
      doing: 3,
      blocked: 0,
      review: 1,
      done: 4,
    },
    priority_distribution: {
      critical: 1,
      high: 2,
      medium: 3,
      low: 4,
      someday: 0,
    },
    completion_rate: 40,
    top_assignees: [],
    tasks_created_last_7d: 2,
    tasks_completed_last_7d: 4,
    velocity_trend: [{ date: '2026-04-13', value: 4 }],
    projects_summary: [],
  };

  beforeEach(() => {
    hooks.useCreateEntity.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    });
    hooks.useHealth.mockReturnValue({
      data: {
        status: 'healthy',
        server_name: 'sibyl',
        uptime_seconds: 123,
        graph_connected: true,
        entity_counts: {},
        errors: [],
      },
      isLoading: false,
    });
    hooks.useStats.mockReturnValue({ data: initialStats });
    hooks.useProjects.mockReturnValue({
      data: {
        entities: [
          { id: 'proj_1', name: 'Alpha' },
          { id: 'proj_2', name: 'Beta' },
        ],
      },
    });
    hooks.useOrgMetrics.mockReturnValue({ data: orgMetrics });
    hooks.useTasks.mockReset();
  });

  it('renders task overview from org metrics without fetching task entities', () => {
    render(<DashboardContent initialStats={initialStats} />);

    expect(screen.getByText('Task Overview')).toBeInTheDocument();
    expect(screen.getByText('3 in progress')).toBeInTheDocument();
    expect(screen.getByText('40% complete')).toBeInTheDocument();
    expect(hooks.useTasks).not.toHaveBeenCalled();
  });

  it('surfaces a capture-first quick action', () => {
    render(<DashboardContent initialStats={initialStats} />);

    expect(screen.getByRole('button', { name: /capture memory/i })).toBeInTheDocument();
  });
});
