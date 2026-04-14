import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';

const hooks = vi.hoisted(() => ({
  useHealth: vi.fn(),
  useJobs: vi.fn(),
  useRunMaintenanceJob: vi.fn(),
  useStats: vi.fn(),
}));

const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));

vi.mock('@/lib/hooks', () => hooks);
vi.mock('sonner', () => ({ toast }));

import SystemStatusPage from './page';

describe('SystemStatusPage', () => {
  beforeEach(() => {
    toast.success.mockReset();
    toast.error.mockReset();

    hooks.useHealth.mockReturnValue({
      data: {
        status: 'healthy',
        server_name: 'sibyl',
        uptime_seconds: 321,
        graph_connected: true,
        errors: [],
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    hooks.useStats.mockReturnValue({
      data: {
        total_entities: 2908,
        entity_counts: {
          task: 1505,
          episode: 766,
        },
      },
      isLoading: false,
    });
    hooks.useJobs.mockReturnValue({
      data: {
        jobs: [
          {
            job_id: 'consolidate:org-123',
            function: 'consolidate_org',
            status: 'complete',
            enqueue_time: '2026-04-14T16:00:00Z',
            start_time: '2026-04-14T16:00:02Z',
            finish_time: '2026-04-14T16:00:04Z',
            error: null,
          },
          {
            job_id: 'priority_decay:org-123',
            function: 'priority_decay',
            status: 'queued',
            enqueue_time: '2026-04-14T15:30:00Z',
            start_time: null,
            finish_time: null,
            error: null,
          },
        ],
        total: 2,
      },
      isLoading: false,
    });
    hooks.useRunMaintenanceJob.mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue({
        job_id: 'consolidate:org-123',
        function: 'consolidate_org',
        status: 'queued',
        message: 'Consolidation run queued',
      }),
      isPending: false,
    });
  });

  it('renders maintenance controls and recent activity', () => {
    render(<SystemStatusPage />);

    expect(screen.getByText('Memory Maintenance')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /run consolidation/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /run forgetting sweep/i })).toBeInTheDocument();
    expect(screen.getByText('Recent Activity')).toBeInTheDocument();
    expect(screen.getAllByText('Consolidation').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Forgetting Sweep').length).toBeGreaterThan(0);
  });

  it('queues a consolidation run from the admin panel', async () => {
    const mutateAsync = vi.fn().mockResolvedValue({
      job_id: 'consolidate:org-123',
      function: 'consolidate_org',
      status: 'queued',
      message: 'Consolidation run queued',
    });
    hooks.useRunMaintenanceJob.mockReturnValue({
      mutateAsync,
      isPending: false,
    });

    const { user } = render(<SystemStatusPage />);

    await user.click(screen.getByRole('button', { name: /run consolidation/i }));

    expect(mutateAsync).toHaveBeenCalledWith({ action: 'consolidate' });
    expect(toast.success).toHaveBeenCalledWith('Consolidation run queued');
  });
});
