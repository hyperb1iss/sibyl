import { act, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
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
  useRevealProject,
} from './project-context';

const PROJECTS = {
  entities: [
    { id: 'project_old', name: 'Old', metadata: { last_activity_at: '2026-01-01T00:00:00Z' } },
    { id: 'project_new', name: 'New', metadata: { last_activity_at: '2026-09-01T00:00:00Z' } },
  ],
};

const ARCHIVED = {
  id: 'project_archived',
  name: 'Archived',
  metadata: { status: 'archived', last_activity_at: '2026-09-24T00:00:00Z' },
};
const WITH_ARCHIVED = { entities: [...PROJECTS.entities, ARCHIVED] };

/** Answer like the API: archived projects only when the caller asks for them. */
function projectsByArchive(options?: { includeArchived?: boolean }) {
  return { data: options?.includeArchived ? WITH_ARCHIVED : PROJECTS, isError: false };
}

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

function RevealProbe() {
  const reveal = useRevealProject();
  const { selectedProjects, isAll } = useProjectContext();
  const [changed, setChanged] = useState<string>('none');
  return (
    <div>
      <span data-testid="reveal-selected">{selectedProjects.join(',')}</span>
      <span data-testid="reveal-all">{String(isAll)}</span>
      <span data-testid="reveal-changed">{changed}</span>
      <button type="button" onClick={() => setChanged(String(reveal('project_old')))}>
        reveal old
      </button>
      <button type="button" onClick={() => setChanged(String(reveal('project_new')))}>
        reveal new
      </button>
    </div>
  );
}

function renderRevealProbe() {
  return render(
    <ProjectContextProvider>
      <RevealProbe />
    </ProjectContextProvider>
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

  it('falls back to every project for an empty org without saving it as a choice', async () => {
    hooks.useProjects.mockReturnValue({ data: { entities: [] }, isError: false });

    const first = renderProbe();

    await waitFor(() => expect(screen.getByTestId('all').textContent).toBe('true'));
    expect(localStorage.getItem('sibyl-project-context')).toBeNull();
    first.unmount();

    // Next visit, projects exist now: it still opens on one
    hooks.useProjects.mockReturnValue({ data: PROJECTS, isError: false });
    renderProbe();
    await waitFor(() => expect(screen.getByTestId('selected').textContent).toBe('project_new'));
    expect(screen.getByTestId('all').textContent).toBe('false');
  });

  it('falls back to every project when projects fail to load, without saving it', async () => {
    hooks.useProjects.mockReturnValue({ data: undefined, isError: true });

    const first = renderProbe();

    await waitFor(() => expect(screen.getByTestId('all').textContent).toBe('true'));
    expect(screen.getByTestId('ready').textContent).toBe('true');
    expect(localStorage.getItem('sibyl-project-context')).toBeNull();
    first.unmount();

    // A healthy backend on the next visit adopts a project
    hooks.useProjects.mockReturnValue({ data: PROJECTS, isError: false });
    renderProbe();
    await waitFor(() => expect(screen.getByTestId('selected').textContent).toBe('project_new'));
  });

  it('drops a stored project that no longer exists and opens on the default', async () => {
    localStorage.setItem('sibyl-project-context', JSON.stringify({ projects: ['project_gone'] }));

    renderProbe();

    await waitFor(() => expect(screen.getByTestId('selected').textContent).toBe('project_new'));
    expect(JSON.parse(localStorage.getItem('sibyl-project-context') ?? 'null')).toEqual({
      projects: ['project_new'],
    });
  });

  it('keeps the stored projects that still exist and drops the rest', async () => {
    localStorage.setItem(
      'sibyl-project-context',
      JSON.stringify({ projects: ['project_gone', 'project_old'] })
    );

    renderProbe();

    await waitFor(() => expect(screen.getByTestId('selected').textContent).toBe('project_old'));
  });

  it('keeps a stored choice when the project list fails to load', () => {
    localStorage.setItem('sibyl-project-context', JSON.stringify({ projects: ['project_old'] }));
    hooks.useProjects.mockReturnValue({ data: undefined, isError: true });

    renderProbe();

    expect(screen.getByTestId('selected').textContent).toBe('project_old');
    expect(screen.getByTestId('all').textContent).toBe('false');
  });

  it('re-scopes when the project list changes to another org', async () => {
    localStorage.setItem('sibyl-project-context', JSON.stringify({ projects: ['project_old'] }));

    const view = renderProbe();
    await waitFor(() => expect(screen.getByTestId('selected').textContent).toBe('project_old'));

    hooks.useProjects.mockReturnValue({
      data: {
        entities: [
          {
            id: 'project_other_org',
            name: 'Other',
            metadata: { updated_at: '2026-09-20T00:00:00Z' },
          },
        ],
      },
      isError: false,
    });
    view.rerender(
      <ProjectContextProvider>
        <Probe />
      </ProjectContextProvider>
    );

    await waitFor(() =>
      expect(screen.getByTestId('selected').textContent).toBe('project_other_org')
    );
  });

  it('keeps an archived project that a link put in the URL', async () => {
    navigation.params = new URLSearchParams('projects=project_archived');
    hooks.useProjects.mockImplementation(projectsByArchive);

    renderProbe();

    await waitFor(() => expect(screen.getByTestId('ready').textContent).toBe('true'));
    // Give validation a chance to run against the list, then check it held
    await waitFor(() =>
      expect(hooks.useProjects).toHaveBeenCalledWith(
        expect.objectContaining({ includeArchived: true })
      )
    );
    expect(screen.getByTestId('selected').textContent).toBe('project_archived');
    expect(navigation.replace).not.toHaveBeenCalled();
  });

  it('opens a dropped selection on the most recent active project, never an archived one', async () => {
    localStorage.setItem('sibyl-project-context', JSON.stringify({ projects: ['project_gone'] }));
    hooks.useProjects.mockImplementation(projectsByArchive);

    renderProbe();

    await waitFor(() => expect(screen.getByTestId('selected').textContent).toBe('project_new'));
  });

  it('rewrites a corrected URL selection when the project list is already cached', async () => {
    navigation.params = new URLSearchParams('projects=project_deleted');

    renderProbe();

    await waitFor(() => expect(screen.getByTestId('selected').textContent).toBe('project_new'));
    await waitFor(() =>
      expect(navigation.replace).toHaveBeenCalledWith('/tasks?projects=project_new', {
        scroll: false,
      })
    );
    expect(navigation.replace).not.toHaveBeenCalledWith('/tasks?projects=project_deleted', {
      scroll: false,
    });
  });

  it('keeps a stored project missing from a truncated project list', async () => {
    localStorage.setItem('sibyl-project-context', JSON.stringify({ projects: ['project_far'] }));
    hooks.useProjects.mockReturnValue({ data: { ...PROJECTS, has_more: true }, isError: false });

    renderProbe();

    await waitFor(() => expect(screen.getByTestId('ready').textContent).toBe('true'));
    await waitFor(() => expect(hooks.useProjects).toHaveBeenCalled());
    expect(screen.getByTestId('selected').textContent).toBe('project_far');
  });

  it('saves every project when the URL asks for it on purpose', async () => {
    navigation.params = new URLSearchParams('projects=all');

    renderProbe();

    await waitFor(() => expect(screen.getByTestId('all').textContent).toBe('true'));
    expect(JSON.parse(localStorage.getItem('sibyl-project-context') ?? 'null')).toEqual({
      mode: 'all',
    });
  });

  it('keeps cross-project pages unscoped and ready', () => {
    navigation.pathname = '/settings';

    renderProbe();

    expect(screen.getByTestId('ready').textContent).toBe('true');
    expect(screen.getByTestId('filters').textContent).toBe('none');
  });
});

describe('useRevealProject', () => {
  beforeEach(() => {
    localStorage.clear();
    navigation.replace.mockReset();
    navigation.pathname = '/tasks';
    navigation.params = new URLSearchParams();
    hooks.useProjects.mockReset();
    hooks.useProjects.mockReturnValue({ data: PROJECTS, isError: false });
  });

  it('adds a project outside the selection so a new item stays in view', async () => {
    renderRevealProbe();
    await waitFor(() =>
      expect(screen.getByTestId('reveal-selected').textContent).toBe('project_new')
    );

    act(() => {
      screen.getByRole('button', { name: 'reveal old' }).click();
    });

    await waitFor(() =>
      expect(screen.getByTestId('reveal-selected').textContent).toBe('project_new,project_old')
    );
    expect(screen.getByTestId('reveal-changed').textContent).toBe('true');
  });

  it('leaves the selection alone when the project is already in it', async () => {
    renderRevealProbe();
    await waitFor(() =>
      expect(screen.getByTestId('reveal-selected').textContent).toBe('project_new')
    );

    act(() => {
      screen.getByRole('button', { name: 'reveal new' }).click();
    });

    expect(screen.getByTestId('reveal-changed').textContent).toBe('false');
    expect(screen.getByTestId('reveal-selected').textContent).toBe('project_new');
  });

  it('keeps an explicit every-project choice intact', () => {
    localStorage.setItem('sibyl-project-context', JSON.stringify({ mode: 'all' }));
    renderRevealProbe();

    act(() => {
      screen.getByRole('button', { name: 'reveal old' }).click();
    });

    expect(screen.getByTestId('reveal-changed').textContent).toBe('false');
    expect(screen.getByTestId('reveal-all').textContent).toBe('true');
  });

  it('keeps a fallback every-project view intact', async () => {
    hooks.useProjects.mockReturnValue({ data: undefined, isError: true });
    renderRevealProbe();
    await waitFor(() => expect(screen.getByTestId('reveal-all').textContent).toBe('true'));

    act(() => {
      screen.getByRole('button', { name: 'reveal old' }).click();
    });

    expect(screen.getByTestId('reveal-changed').textContent).toBe('false');
    expect(screen.getByTestId('reveal-all').textContent).toBe('true');
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
