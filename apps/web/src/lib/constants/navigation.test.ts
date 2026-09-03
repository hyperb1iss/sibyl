import { describe, expect, it } from 'vitest';

import { NAVIGATION, NAVIGATION_SECTIONS, ROUTE_CONFIG, withProjectsContext } from './navigation';

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

describe('navigation sections', () => {
  it('groups navigation into overview, work, memory, explore, and system', () => {
    expect(NAVIGATION_SECTIONS.map(section => section.id)).toEqual([
      'overview',
      'work',
      'memory',
      'explore',
      'system',
    ]);
    expect(NAVIGATION_SECTIONS.find(s => s.id === 'work')?.items.map(i => i.href)).toEqual([
      '/projects',
      '/epics',
      '/tasks',
    ]);
    expect(NAVIGATION_SECTIONS.find(s => s.id === 'memory')?.items.map(i => i.href)).toEqual([
      '/memory',
      '/sources',
      '/entities',
      '/graph',
    ]);
  });

  it('surfaces settings as a system navigation item', () => {
    expect(NAVIGATION.map(item => item.href)).toContain('/settings');
    expect(NAVIGATION_SECTIONS.find(s => s.id === 'system')?.items).toEqual([
      expect.objectContaining({ name: 'Settings', href: '/settings' }),
    ]);
  });

  it('keeps every navigation item inside exactly one section', () => {
    const sectioned = NAVIGATION_SECTIONS.flatMap(section => section.items.map(i => i.href));
    expect([...sectioned].sort()).toEqual(NAVIGATION.map(item => item.href).sort());
    expect(new Set(sectioned).size).toBe(sectioned.length);
  });
});
