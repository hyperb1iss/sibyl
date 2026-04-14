import { describe, expect, it } from 'vitest';

import { NAVIGATION, withProjectsContext } from './navigation';

describe('navigation constants', () => {
  it('keeps epics in the shared navigation list', () => {
    expect(NAVIGATION.map(item => item.href)).toContain('/epics');
  });

  it('surfaces the raw archive in shared navigation', () => {
    expect(NAVIGATION.map(item => item.href)).toContain('/archive');
  });

  it('preserves project context when navigating', () => {
    expect(withProjectsContext('/tasks', 'proj-a,proj-b')).toBe('/tasks?projects=proj-a,proj-b');
    expect(withProjectsContext('/search?view=all', 'proj-a')).toBe(
      '/search?view=all&projects=proj-a'
    );
    expect(withProjectsContext('/graph', null)).toBe('/graph');
  });
});
