import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { writeStorage } from '@/lib/storage';
import { projectFilterTarget, render } from '@/test/utils';

const navigation = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  searchParams: '',
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: navigation.push, replace: navigation.replace }),
  usePathname: () => '/epics',
  useSearchParams: () => new URLSearchParams(navigation.searchParams),
}));

vi.mock('@/lib/hooks', () => ({
  useEpics: () => ({ data: { entities: [] }, isLoading: false, error: null }),
  useProjects: () => ({ data: { entities: [] } }),
}));

vi.mock('@/lib/project-context', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/project-context')>()),
  useProjectFilters: () => ['proj-a'],
}));

import EpicsPage from './page';

describe('EpicsPage stored filters', () => {
  beforeEach(() => {
    navigation.push.mockReset();
    navigation.replace.mockReset();
    navigation.searchParams = '';
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('restores stored filters without dropping the project scope', () => {
    writeStorage('epics:filters', { status: 'planning' });
    navigation.searchParams = 'projects=proj-a';

    render(<EpicsPage />);

    expect(navigation.replace).toHaveBeenCalledTimes(1);
    const [href] = navigation.replace.mock.calls[0];
    expect(projectFilterTarget(href)).toEqual({ pathname: '/epics', projects: ['proj-a'] });
    expect(new URL(href, 'http://localhost').searchParams.get('status')).toBe('planning');
  });

  it('leaves the URL alone when nothing is stored', () => {
    navigation.searchParams = 'projects=proj-a';

    render(<EpicsPage />);

    expect(navigation.replace).not.toHaveBeenCalled();
  });
});
