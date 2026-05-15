import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';

const navigationState = vi.hoisted(() => ({
  searchParams: new URLSearchParams(),
}));

const hooks = vi.hoisted(() => ({
  useMemoryAudit: vi.fn(),
  useMemorySpaces: vi.fn(),
  useRawCaptures: vi.fn(),
}));

vi.mock('next/navigation', () => ({
  usePathname: () => '/memory',
  useSearchParams: () => navigationState.searchParams,
}));

vi.mock('@/lib/hooks', () => hooks);

import { MemoryContent } from './memory-content';

const baseCapture = {
  entity_id: null,
  entity_type: 'episode',
  tags: [],
  created_by_user_id: 'user-1',
  created_at: '2026-05-14T12:00:00Z',
  review_state: 'pending' as const,
};

const captures = {
  captures: [
    {
      ...baseCapture,
      id: 'raw-private',
      title: 'Terminal capture',
      metadata: { memory_scope: 'private' },
      capture_surface: 'cli',
    },
    {
      ...baseCapture,
      id: 'raw-project',
      title: 'Project runbook note',
      metadata: { memory_scope: 'project' },
      capture_surface: 'dashboard',
    },
  ],
  limit: 24,
  offset: 0,
  has_more: false,
};

const pendingCaptures = {
  ...captures,
  captures: [
    {
      ...baseCapture,
      id: 'review-project',
      title: 'Review policy update',
      metadata: { memory_scope: 'project' },
      capture_surface: 'reflection_candidate',
    },
  ],
};

const importCaptures = {
  ...captures,
  captures: [
    {
      ...baseCapture,
      id: 'import-project',
      title: 'Mailbox import',
      metadata: { memory_scope: 'project', adapter_name: 'mbox' },
      capture_surface: 'source_import',
    },
  ],
};

const reflectionCaptures = {
  ...captures,
  captures: [
    {
      ...baseCapture,
      id: 'reflection-project',
      title: 'Decision candidate',
      metadata: { memory_scope: 'project' },
      capture_surface: 'reflection_candidate',
    },
  ],
};

const audit = {
  events: [
    {
      id: 'audit-recall',
      organization_id: 'org-1',
      user_id: 'user-1',
      action: 'memory.recall',
      memory_scope: 'project',
      scope_key: 'project-a',
      project_id: 'project-a',
      source_surface: 'raw_recall',
      source_ids: ['raw-project'],
      source_ids_truncated: null,
      derived_ids: ['raw-project'],
      derived_ids_truncated: null,
      policy_allowed: true,
      policy_reason: 'same_scope_read_allowed',
      details: {},
      created_at: '2026-05-14T12:05:00Z',
    },
    {
      id: 'audit-access',
      organization_id: 'org-1',
      user_id: 'user-1',
      action: 'memory.access.preview',
      memory_scope: 'project',
      scope_key: 'project-a',
      project_id: 'project-a',
      source_surface: 'memory_access_preview',
      source_ids: ['raw-project'],
      source_ids_truncated: null,
      derived_ids: ['space-1'],
      derived_ids_truncated: null,
      policy_allowed: true,
      policy_reason: 'same_scope_read_allowed',
      details: {},
      created_at: '2026-05-14T12:06:00Z',
    },
    {
      id: 'audit-private',
      organization_id: 'org-1',
      user_id: 'user-1',
      action: 'memory.remember',
      memory_scope: 'private',
      scope_key: null,
      project_id: null,
      source_surface: 'cli',
      source_ids: ['raw-private'],
      source_ids_truncated: null,
      derived_ids: ['raw-private'],
      derived_ids_truncated: null,
      policy_allowed: true,
      policy_reason: 'same_scope_write_allowed',
      details: {},
      created_at: '2026-05-14T12:07:00Z',
    },
  ],
  limit: 50,
};

const spaces = {
  spaces: [
    {
      id: 'space-1',
      organization_id: 'org-1',
      memory_scope: 'project' as const,
      scope_key: 'project-a',
      name: 'Project Memory',
      description: null,
      state: 'active' as const,
      disabled_reason: null,
      metadata: {},
      created_by_user_id: 'user-1',
      created_at: '2026-05-14T12:00:00Z',
      updated_at: '2026-05-14T12:00:00Z',
      members: [
        {
          id: 'member-1',
          organization_id: 'org-1',
          space_id: 'space-1',
          principal_type: 'agent',
          principal_id: 'codex',
          role: 'reader',
          permissions: ['recall'],
          expires_at: null,
          created_by_user_id: 'user-1',
          created_at: '2026-05-14T12:00:00Z',
          updated_at: '2026-05-14T12:00:00Z',
        },
      ],
    },
  ],
};

describe('MemoryContent', () => {
  beforeEach(() => {
    navigationState.searchParams = new URLSearchParams();
    hooks.useRawCaptures.mockImplementation((params?: Record<string, unknown>) => {
      if (params?.capture_surface === 'source_import') {
        return { data: importCaptures, isLoading: false, error: null };
      }
      if (params?.capture_surface === 'reflection_candidate') {
        return { data: reflectionCaptures, isLoading: false, error: null };
      }
      if ('review_state' in (params ?? {})) {
        return { data: pendingCaptures, isLoading: false, error: null };
      }
      return { data: captures, isLoading: false, error: null };
    });
    hooks.useMemoryAudit.mockReturnValue({ data: audit, isLoading: false, error: null });
    hooks.useMemorySpaces.mockReturnValue({ data: spaces, isLoading: false, error: null });
  });

  it('renders the memory workspace home panels', () => {
    render(<MemoryContent />);

    expect(screen.getByText('Memory Workspace')).toBeInTheDocument();
    expect(screen.getByText('Recent Captures')).toBeInTheDocument();
    expect(screen.getByText('Review Actions')).toBeInTheDocument();
    expect(screen.getByText('Recent Imports')).toBeInTheDocument();
    expect(screen.getByText('Reflection Queue')).toBeInTheDocument();
    expect(screen.getByText('Agent Access')).toBeInTheDocument();
    expect(screen.getByText('Terminal capture')).toBeInTheDocument();
    expect(screen.getByText('Mailbox import')).toBeInTheDocument();
    expect(screen.getByText('Decision candidate')).toBeInTheDocument();
    expect(screen.getByText('agent:codex')).toBeInTheDocument();
  });

  it('filters workspace panels by memory scope', async () => {
    const { user } = render(<MemoryContent />);

    await user.click(screen.getByRole('button', { name: /project/i }));

    expect(screen.getByText('Project runbook note')).toBeInTheDocument();
    expect(screen.queryByText('Terminal capture')).not.toBeInTheDocument();
    expect(screen.getAllByText('same_scope_read_allowed').length).toBeGreaterThan(0);
  });
});
