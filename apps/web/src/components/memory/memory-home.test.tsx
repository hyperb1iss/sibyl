import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';

const hooks = vi.hoisted(() => ({
  useRawCaptures: vi.fn(),
  useMemoryAudit: vi.fn(),
  useMemorySpaces: vi.fn(),
}));

vi.mock('@/lib/hooks/memory', () => hooks);
vi.mock('@/lib/project-context', () => ({
  useProjectContext: () => ({ selectedProjects: ['project_1'], isAll: false, scopeReady: true }),
}));

import { MemoryHome } from './memory-home';

function capture(id: string, memoryScope: string) {
  return {
    id,
    entity_id: `decision_${id}`,
    title: `Memory ${id}`,
    entity_type: 'decision',
    tags: [],
    metadata: { capture_mode: 'remember', capture_surface: 'cli' },
    capture_surface: 'cli',
    review_state: 'pending',
    memory_scope: memoryScope,
    project_id: 'project_1',
    created_by_user_id: 'user-1',
    created_at: '2026-10-30T16:00:00Z',
  };
}

function scopeTab(label: string, count: number): HTMLElement {
  return screen.getByRole('button', { name: new RegExp(`^${label}\\s*${count}$`) });
}

describe('MemoryHome', () => {
  it('counts captures by the scope the API reports, not request metadata', () => {
    hooks.useRawCaptures.mockReturnValue({
      data: {
        captures: [capture('a', 'private'), capture('b', 'private'), capture('c', 'project')],
        limit: 24,
        offset: 0,
        has_more: false,
      },
      isLoading: false,
      error: null,
    });
    hooks.useMemoryAudit.mockReturnValue({ data: { events: [] }, isLoading: false, error: null });
    hooks.useMemorySpaces.mockReturnValue({ data: { spaces: [] }, isLoading: false, error: null });

    render(<MemoryHome />);

    expect(scopeTab('All', 3)).toBeInTheDocument();
    expect(scopeTab('Private', 2)).toBeInTheDocument();
    expect(scopeTab('Project', 1)).toBeInTheDocument();
  });
});
