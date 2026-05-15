import { describe, expect, it } from 'vitest';

import { NAVIGATION, ROUTE_CONFIG, withProjectsContext } from './navigation';

describe('navigation constants', () => {
  it('derives dashboard navigation from the shared route config', () => {
    expect(ROUTE_CONFIG[''].label).toBe('Home');
    expect(NAVIGATION[0]).toMatchObject({ name: 'Dashboard', href: '/' });
  });

  it('keeps epics in the shared navigation list', () => {
    expect(NAVIGATION.map(item => item.href)).toContain('/epics');
  });

  it('keeps the legacy archive path as a hidden memory captures redirect', () => {
    expect(ROUTE_CONFIG.archive).toMatchObject({
      label: 'Memory Captures',
      href: '/archive',
    });
    expect(NAVIGATION.map(item => item.href)).not.toContain('/archive');
  });

  it('surfaces the memory workspace in shared navigation', () => {
    expect(ROUTE_CONFIG.memory.label).toBe('Memory');
    expect(NAVIGATION.map(item => item.href)).toContain('/memory');
  });

  it('does not expose a dead top-level documents route', () => {
    expect(ROUTE_CONFIG.documents).toBeUndefined();
  });

  it('preserves project context when navigating', () => {
    expect(withProjectsContext('/tasks', 'proj-a,proj-b')).toBe('/tasks?projects=proj-a,proj-b');
    expect(withProjectsContext('/search?view=all', 'proj-a')).toBe(
      '/search?view=all&projects=proj-a'
    );
    expect(withProjectsContext('/graph', null)).toBe('/graph');
  });
});
