import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ProjectSummariesResponse, TaskListResponse } from '@/lib/api';
import { render, screen } from '@/test/utils';

const push = vi.fn();

const hooks = vi.hoisted(() => ({
  useDeleteEntity: vi.fn(),
  useCreateEntity: vi.fn(),
  useMe: vi.fn(),
  useProjectMembers: vi.fn(),
  useProjectMetrics: vi.fn(),
  useProjectSummaries: vi.fn(),
  useProjects: vi.fn(),
  useRemoveProjectMember: vi.fn(),
  useTasks: vi.fn(),
  useUpdateEntity: vi.fn(),
  useUpdateProjectMemberRole: vi.fn(),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push }),
  usePathname: () => '/projects',
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock('@/lib/hooks', () => hooks);

import { ProjectsContent } from './projects-content';

describe('ProjectsContent', () => {
  const initialProjects: TaskListResponse = {
    mode: 'list',
    entities: [
      {
        id: 'proj-a',
        type: 'project',
        name: 'Alpha',
        description: 'First project',
        metadata: {},
      },
      {
        id: 'proj-b',
        type: 'project',
        name: 'Beta',
        description: 'Second project',
        metadata: {},
      },
    ],
    total: 2,
    filters: {},
  };

  const initialProjectSummaries: ProjectSummariesResponse = {
    projects_summary: [
      {
        id: 'proj-a',
        name: 'Alpha',
        total: 2,
        completed: 1,
        doing: 1,
        blocked: 0,
        review: 0,
        todo: 0,
        backlog: 0,
        critical: 0,
        high: 0,
        overdue: 0,
        completion_rate: 50,
      },
      {
        id: 'proj-b',
        name: 'Beta',
        total: 3,
        completed: 1,
        doing: 0,
        blocked: 1,
        review: 1,
        todo: 0,
        backlog: 0,
        critical: 1,
        high: 0,
        overdue: 0,
        completion_rate: 33.3,
      },
    ],
  };

  beforeEach(() => {
    push.mockReset();
    hooks.useDeleteEntity.mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
    hooks.useCreateEntity.mockReturnValue({ mutateAsync: vi.fn() });
    hooks.useMe.mockReturnValue({ data: undefined });
    hooks.useProjectMembers.mockReturnValue({ data: { members: [] } });
    hooks.useProjectMetrics.mockReturnValue({ data: undefined });
    hooks.useProjectSummaries.mockReturnValue({
      data: initialProjectSummaries,
      isLoading: false,
    });
    hooks.useProjects.mockReturnValue({
      data: initialProjects,
      isLoading: false,
      error: null,
    });
    hooks.useRemoveProjectMember.mockReturnValue({ mutateAsync: vi.fn() });
    hooks.useTasks.mockReturnValue({ data: { entities: [] }, isLoading: false });
    hooks.useUpdateEntity.mockReturnValue({ mutateAsync: vi.fn() });
    hooks.useUpdateProjectMemberRole.mockReturnValue({ mutateAsync: vi.fn() });
  });

  it('uses lean project summaries to render sidebar stats', () => {
    render(
      <ProjectsContent
        initialProjects={initialProjects}
        initialProjectSummaries={initialProjectSummaries}
      />
    );

    expect(hooks.useProjectSummaries).toHaveBeenCalledWith(initialProjectSummaries);
    expect(screen.getByText('2 projects | 5 tasks | 2 active')).toBeInTheDocument();
    expect(screen.getByText('Beta')).toBeInTheDocument();
  });
});
