import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';

const hooks = vi.hoisted(() => ({
  useBackupSettings: vi.fn(),
  useBackups: vi.fn(),
  useCreateBackup: vi.fn(),
  useDeleteBackup: vi.fn(),
  useUpdateBackupSettings: vi.fn(),
}));

vi.mock('@/lib/hooks', () => hooks);

import BackupsPage from './page';

describe('BackupsPage', () => {
  beforeEach(() => {
    hooks.useBackupSettings.mockReturnValue({
      data: {
        enabled: true,
        schedule: '0 2 * * *',
        retention_days: 30,
        include_database_dump: true,
        include_graph: true,
        database_dump_supported: false,
        archive_contents: ['auth.json', 'content.json', 'graph.json', 'metadata.json'],
        last_backup_at: '2026-04-14T16:00:00Z',
        last_backup_id: 'backup_123',
      },
      isLoading: false,
    });
    hooks.useBackups.mockReturnValue({
      data: {
        total: 2,
        backups: [
          {
            id: 'row_1',
            backup_id: 'backup_123',
            status: 'completed',
            filename: 'backup_123.tar.gz',
            size_bytes: 1024,
            entity_count: 42,
            relationship_count: 21,
            duration_seconds: 12,
            triggered_by: 'manual',
            created_at: '2026-04-14T16:00:00Z',
            started_at: '2026-04-14T16:00:00Z',
            completed_at: '2026-04-14T16:00:12Z',
            error: null,
          },
          {
            id: 'row_2',
            backup_id: 'backup_124',
            status: 'in_progress',
            filename: null,
            size_bytes: 0,
            entity_count: 0,
            relationship_count: 0,
            duration_seconds: 0,
            triggered_by: 'scheduled',
            created_at: '2026-04-14T17:00:00Z',
            started_at: '2026-04-14T17:00:01Z',
            completed_at: null,
            error: null,
          },
        ],
      },
      isLoading: false,
      refetch: vi.fn(),
    });
    hooks.useUpdateBackupSettings.mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue({}),
      isPending: false,
    });
    hooks.useCreateBackup.mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue({}),
      isPending: false,
    });
    hooks.useDeleteBackup.mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue({}),
      isPending: false,
    });
  });

  it('renders tracked backup management controls and archive rows', () => {
    render(<BackupsPage />);

    expect(screen.getByRole('heading', { name: 'Backup Management' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /create backup/i })).toBeInTheDocument();
    expect(screen.getByText('Scheduled Backups')).toBeInTheDocument();
    expect(screen.getByText('Surreal Data Snapshot')).toBeInTheDocument();
    expect(screen.getByText('Archives (2)')).toBeInTheDocument();
    expect(screen.getByText('backup_123')).toBeInTheDocument();
    expect(screen.getByText('backup_124')).toBeInTheDocument();
    expect(screen.getByText('In Progress')).toBeInTheDocument();
  });
});
