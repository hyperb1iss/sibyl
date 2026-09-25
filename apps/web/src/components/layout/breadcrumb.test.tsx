import { beforeEach, describe, expect, it, vi } from 'vitest';
import { projectFilterTarget, render, screen } from '@/test/utils';
import { Breadcrumb, EntityBreadcrumb } from './breadcrumb';
import { BreadcrumbProvider } from './breadcrumb-context';

const navigationState = vi.hoisted(() => ({
  pathname: '/tasks/task-1',
  searchParams: new URLSearchParams(),
}));

vi.mock('next/navigation', () => ({
  usePathname: () => navigationState.pathname,
  useSearchParams: () => navigationState.searchParams,
}));

const alpha = { id: 'proj-a', name: 'Alpha' };

function renderTrail(entityType: 'task' | 'epic') {
  return render(
    <BreadcrumbProvider>
      <Breadcrumb />
      <EntityBreadcrumb entityType={entityType} entityName="Detail" parentProject={alpha} />
    </BreadcrumbProvider>
  );
}

describe('EntityBreadcrumb parent project crumb', () => {
  beforeEach(() => {
    navigationState.pathname = '/tasks/task-1';
    navigationState.searchParams = new URLSearchParams();
  });

  it('scopes the task list to the parent project', () => {
    renderTrail('task');

    const crumb = screen.getByRole('link', { name: 'Alpha' });
    expect(projectFilterTarget(crumb.getAttribute('href'))).toEqual({
      pathname: '/tasks',
      projects: ['proj-a'],
    });
  });

  it('scopes the epic list to the parent project', () => {
    navigationState.pathname = '/epics/epic-1';
    renderTrail('epic');

    const crumb = screen.getByRole('link', { name: 'Alpha' });
    expect(projectFilterTarget(crumb.getAttribute('href'))).toEqual({
      pathname: '/epics',
      projects: ['proj-a'],
    });
  });

  it('targets the parent project over the ambient selection', () => {
    navigationState.searchParams = new URLSearchParams('projects=proj-b');
    renderTrail('task');

    expect(screen.getByRole('link', { name: 'Alpha' })).toHaveAttribute(
      'href',
      '/tasks?projects=proj-a'
    );
    expect(screen.getByRole('link', { name: 'Tasks' })).toHaveAttribute(
      'href',
      '/tasks?projects=proj-b'
    );
  });
});
