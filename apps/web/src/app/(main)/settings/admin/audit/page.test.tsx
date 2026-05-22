import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';

const hooks = vi.hoisted(() => ({
  useAdminAudit: vi.fn(),
}));

const apiExport = vi.hoisted(() => vi.fn());

vi.mock('@/lib/hooks', () => hooks);
vi.mock('@/lib/api', async importOriginal => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      admin: {
        ...actual.api.admin,
        audit: {
          ...actual.api.admin.audit,
          export: apiExport,
        },
      },
    },
  };
});

import AdminAuditPage from './page';

describe('AdminAuditPage', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  beforeEach(() => {
    apiExport.mockReset();
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:audit');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    hooks.useAdminAudit.mockReturnValue({
      data: {
        total: 1,
        limit: 50,
        offset: 0,
        has_more: false,
        events: [
          {
            id: 'audit-1',
            organization_id: 'org-1',
            user_id: 'user-1',
            action: 'api_key.create',
            resource: 'api_key:abc',
            ip_address: '127.0.0.1',
            user_agent: 'pytest',
            details: { scope: 'api:read' },
            created_at: '2026-05-22T09:30:00Z',
          },
        ],
      },
      isLoading: false,
      isFetching: false,
      error: null,
      refetch: vi.fn(),
    });
    apiExport.mockResolvedValue(new Blob(['audit'], { type: 'application/json' }));
  });

  it('renders filters, audit rows, pagination, and export controls', () => {
    render(<AdminAuditPage />);

    expect(screen.getByRole('heading', { name: 'Audit Log' })).toBeInTheDocument();
    expect(screen.getByLabelText('Action')).toHaveAttribute('placeholder', 'memory.recall');
    expect(screen.getByLabelText('User')).toHaveAttribute('placeholder', 'user UUID');
    expect(screen.getByLabelText('Resource')).toHaveAttribute('placeholder', 'project or source');
    expect(screen.getByText('api_key.create')).toBeInTheDocument();
    expect(screen.getByText('user-1')).toBeInTheDocument();
    expect(screen.getByText('api_key:abc')).toBeInTheDocument();
    expect(screen.getByText('{"scope":"api:read"}')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'CSV' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'JSON' })).toBeInTheDocument();
    expect(screen.getByText('1-1')).toBeInTheDocument();
  });

  it('passes the active filters to JSON export', async () => {
    const { user } = render(<AdminAuditPage />);

    await user.type(screen.getByLabelText('Action'), 'memory.recall');
    await user.type(screen.getByLabelText('User'), 'user-2');
    await user.type(screen.getByLabelText('Resource'), 'project:alpha');
    await user.click(screen.getByRole('button', { name: 'JSON' }));

    expect(apiExport).toHaveBeenCalledWith(
      expect.objectContaining({
        action: 'memory.recall',
        user_id: 'user-2',
        resource: 'project:alpha',
        format: 'json',
      })
    );
  });
});
