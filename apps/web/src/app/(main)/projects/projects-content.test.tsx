import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import { BreadcrumbProvider } from '@/components/layout/breadcrumb-context';
import type { ProjectSummariesResponse, TaskListResponse } from '@/lib/api';
import { projectFilterTarget, render, screen } from '@/test/utils';

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

const navigation = vi.hoisted(() => ({
  push: vi.fn(),
  searchParams: '',
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: navigation.push }),
  usePathname: () => '/projects',
  useSearchParams: () => new URLSearchParams(navigation.searchParams),
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
    navigation.push.mockReset();
    navigation.searchParams = '';
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

  it('renders selected project details without thrashing breadcrumb context', () => {
    navigation.searchParams = 'id=proj-a';

    render(
      <BreadcrumbProvider>
        <Breadcrumb />
        <ProjectsContent
          initialProjects={initialProjects}
          initialProjectSummaries={initialProjectSummaries}
        />
      </BreadcrumbProvider>
    );

    expect(screen.getByText('Tech Stack')).toBeInTheDocument();
    expect(screen.getByText('Team')).toBeInTheDocument();
  });

  it('scopes the project task links to the selected project', () => {
    navigation.searchParams = 'id=proj-a';
    hooks.useTasks.mockReturnValue({
      data: {
        entities: Array.from({ length: 6 }, (_, index) => ({
          id: `task-${index}`,
          type: 'task',
          name: `Active task ${index}`,
          metadata: { status: 'doing' },
        })),
      },
      isLoading: false,
    });

    render(
      <ProjectsContent
        initialProjects={initialProjects}
        initialProjectSummaries={initialProjectSummaries}
      />
    );

    for (const link of [
      screen.getByRole('link', { name: 'Tasks' }),
      screen.getByRole('link', { name: /View all 6 active tasks/ }),
    ]) {
      expect(projectFilterTarget(link.getAttribute('href'))).toEqual({
        pathname: '/tasks',
        projects: ['proj-a'],
      });
    }
  });

  it('scopes the empty project add task link to the selected project', () => {
    navigation.searchParams = 'id=proj-a';
    hooks.useProjectSummaries.mockReturnValue({
      data: { projects_summary: [] },
      isLoading: false,
    });

    render(
      <ProjectsContent
        initialProjects={initialProjects}
        initialProjectSummaries={initialProjectSummaries}
      />
    );

    const link = screen.getByRole('link', { name: 'Add Task' });
    expect(projectFilterTarget(link.getAttribute('href'))).toEqual({
      pathname: '/tasks',
      projects: ['proj-a'],
    });
  });
});
