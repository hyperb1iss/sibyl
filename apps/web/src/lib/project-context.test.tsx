import { act, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const navigation = vi.hoisted(() => ({
  replace: vi.fn(),
  pathname: '/tasks',
  params: new URLSearchParams(),
}));
const hooks = vi.hoisted(() => ({ useProjects: vi.fn() }));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: navigation.replace }),
  usePathname: () => navigation.pathname,
  useSearchParams: () => navigation.params,
}));
vi.mock('@/lib/hooks/work-items', () => ({ useProjects: hooks.useProjects }));

import {
  mostRecentProjectId,
  ProjectContextProvider,
  parseStoredSelection,
  useProjectContext,
  useProjectFilters,
} from './project-context';

const PROJECTS = {
  entities: [
    { id: 'project_old', name: 'Old', metadata: { last_activity_at: '2026-01-01T00:00:00Z' } },
    { id: 'project_new', name: 'New', metadata: { last_activity_at: '2026-09-01T00:00:00Z' } },
  ],
};

function Probe() {
  const { isAll, selectedProjects, scopeReady, clearProjects } = useProjectContext();
  const filters = useProjectFilters();
  return (
    <div>
      <span data-testid="ready">{String(scopeReady)}</span>
      <span data-testid="all">{String(isAll)}</span>
      <span data-testid="selected">{selectedProjects.join(',')}</span>
      <span data-testid="filters">{filters ? filters.join(',') : 'none'}</span>
      <button type="button" onClick={clearProjects}>
        every project
      </button>
    </div>
  );
}

function renderProbe() {
  return render(
    <ProjectContextProvider>
      <Probe />
    </ProjectContextProvider>
  );
}

describe('ProjectContextProvider default scope', () => {
  beforeEach(() => {
    localStorage.clear();
    navigation.replace.mockReset();
    navigation.pathname = '/tasks';
    navigation.params = new URLSearchParams();
    hooks.useProjects.mockReset();
    hooks.useProjects.mockReturnValue({ data: PROJECTS, isError: false });
  });

  it('opens a first visit on the most recently active project, not on every project', async () => {
    renderProbe();

    await waitFor(() => expect(screen.getByTestId('ready').textContent).toBe('true'));
    expect(screen.getByTestId('all').textContent).toBe('false');
    expect(screen.getByTestId('selected').textContent).toBe('project_new');
    expect(screen.getByTestId('filters').textContent).toBe('project_new');
    expect(JSON.parse(localStorage.getItem('sibyl-project-context') ?? 'null')).toEqual({
      projects: ['project_new'],
    });
  });

  it('is not ready while the default is still being chosen', () => {
    hooks.useProjects.mockReturnValue({ data: undefined, isError: false });

    renderProbe();

    expect(screen.getByTestId('ready').textContent).toBe('false');
    expect(screen.getByTestId('all').textContent).toBe('false');
  });

  it('treats a legacy empty selection as unset rather than as every project', async () => {
    localStorage.setItem('sibyl-project-context', JSON.stringify([]));

    renderProbe();

    await waitFor(() => expect(screen.getByTestId('selected').textContent).toBe('project_new'));
  });

  it('remembers every project only when the viewer chose it', async () => {
    renderProbe();
    await waitFor(() => expect(screen.getByTestId('ready').textContent).toBe('true'));

    act(() => {
      screen.getByRole('button', { name: /every project/i }).click();
    });

    await waitFor(() => expect(screen.getByTestId('all').textContent).toBe('true'));
    expect(screen.getByTestId('filters').textContent).toBe('none');
    expect(JSON.parse(localStorage.getItem('sibyl-project-context') ?? 'null')).toEqual({
      mode: 'all',
    });
    expect(navigation.replace).toHaveBeenCalledWith('/tasks?projects=all', { scroll: false });
  });

  it('honours an explicit every-project choice from storage without fetching projects', () => {
    localStorage.setItem('sibyl-project-context', JSON.stringify({ mode: 'all' }));

    renderProbe();

    expect(screen.getByTestId('all').textContent).toBe('true');
    expect(screen.getByTestId('ready').textContent).toBe('true');
    expect(hooks.useProjects).not.toHaveBeenCalledWith({ enabled: true });
  });

  it('falls back to every project when the org has none to open on', async () => {
    hooks.useProjects.mockReturnValue({ data: { entities: [] }, isError: false });

    renderProbe();

    await waitFor(() => expect(screen.getByTestId('all').textContent).toBe('true'));
  });

  it('keeps cross-project pages unscoped and ready', () => {
    navigation.pathname = '/settings';

    renderProbe();

    expect(screen.getByTestId('ready').textContent).toBe('true');
    expect(screen.getByTestId('filters').textContent).toBe('none');
  });
});

describe('selection helpers', () => {
  it('reads legacy lists, explicit all, and the new shape', () => {
    expect(parseStoredSelection(null)).toEqual({ kind: 'unset' });
    expect(parseStoredSelection('[]')).toEqual({ kind: 'unset' });
    expect(parseStoredSelection('["p1"]')).toEqual({ kind: 'projects', ids: ['p1'] });
    expect(parseStoredSelection('{"mode":"all"}')).toEqual({ kind: 'all' });
    expect(parseStoredSelection('{"projects":["p2"]}')).toEqual({ kind: 'projects', ids: ['p2'] });
    expect(parseStoredSelection('not json')).toEqual({ kind: 'unset' });
  });

  it('picks the project with the latest activity', () => {
    expect(mostRecentProjectId(PROJECTS.entities)).toBe('project_new');
    expect(mostRecentProjectId([])).toBeNull();
  });
});
