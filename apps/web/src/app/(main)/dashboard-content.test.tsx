import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { OrgMetricsResponse, StatsResponse } from '@/lib/api';
import { render, screen } from '@/test/utils';

const hooks = vi.hoisted(() => ({
  useCreateEntity: vi.fn(),
  useHealth: vi.fn(),
  useOrgMetrics: vi.fn(),
  useProjects: vi.fn(),
  useSessionBundle: vi.fn(),
  useStats: vi.fn(),
  useTasks: vi.fn(),
  useCaptureMemory: vi.fn(),
}));

vi.mock('@/lib/hooks', () => hooks);
vi.mock('@/lib/project-context', () => ({
  useProjectFilters: vi.fn(() => undefined),
}));

vi.mock('@/components/dashboard', () => ({
  WelcomeBanner: () => <div data-testid="welcome-banner" />,
}));
vi.mock('@/components/layout/capture-memory-context', () => hooks);

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
    hooks.useSessionBundle.mockReturnValue({
      data: {
        context: {
          generated_at: '2026-04-15T12:00:00Z',
          org_slug: 'hyper',
          project_ids: [],
          scope: 'all_projects',
        },
        query: 'Fix session bundle | Review archive',
        tasks: [
          {
            id: 'task_1',
            name: 'Fix session bundle',
            status: 'doing',
            priority: 'high',
            feature: null,
            branch_name: null,
          },
        ],
        relevant_entities: [
          {
            id: 'procedure_1',
            name: 'Archive review loop',
            entity_type: 'procedure',
            source: null,
            preview: 'Review the raw archive before you run maintenance jobs.',
            document_id: null,
          },
        ],
        remember_next: 'Continue Fix session bundle and capture anything non-obvious.',
      },
      isLoading: false,
    });
    hooks.useTasks.mockReset();
    hooks.useCaptureMemory.mockReturnValue({
      openCaptureMemory: vi.fn(),
      closeCaptureMemory: vi.fn(),
      isOpen: false,
      captureSurface: 'dashboard',
    });
  });

  it('renders task overview from org metrics without fetching task entities', () => {
    render(<DashboardContent initialStats={initialStats} />);

    expect(screen.getByText('Task Overview')).toBeInTheDocument();
    expect(screen.getByText('3 in progress')).toBeInTheDocument();
    expect(screen.getByText('40% complete')).toBeInTheDocument();
    expect(hooks.useTasks).not.toHaveBeenCalled();
  });

  it('surfaces a capture-first quick action', async () => {
    const openCaptureMemory = vi.fn();
    hooks.useCaptureMemory.mockReturnValue({
      openCaptureMemory,
      closeCaptureMemory: vi.fn(),
      isOpen: false,
      captureSurface: 'dashboard',
    });

    const { user } = render(<DashboardContent initialStats={initialStats} />);

    expect(screen.getByRole('button', { name: /capture memory/i })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /capture memory/i }));

    expect(openCaptureMemory).toHaveBeenCalledWith('dashboard');
  });

  it('links the dashboard to the memory review queue', () => {
    render(<DashboardContent initialStats={initialStats} />);

    expect(screen.getByRole('link', { name: /review memory/i })).toHaveAttribute(
      'href',
      '/memory/captures?link=unlinked'
    );
  });

  it('renders the session snapshot bundle on the dashboard', () => {
    render(<DashboardContent initialStats={initialStats} />);

    expect(screen.getByText('Session Snapshot')).toBeInTheDocument();
    expect(screen.getByText(/continue fix session bundle/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /fix session bundle/i })).toHaveAttribute(
      'href',
      '/tasks/task_1'
    );
    expect(screen.getByText('Archive review loop')).toBeInTheDocument();
  });
});
