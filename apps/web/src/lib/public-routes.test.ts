import { describe, expect, it } from 'vitest';
import { isPublicRoutePath } from './public-routes';

describe('isPublicRoutePath', () => {
  it('matches unauthenticated auth flow pages', () => {
    expect(isPublicRoutePath('/login')).toBe(true);
    expect(isPublicRoutePath('/reset-password')).toBe(true);
    expect(isPublicRoutePath('/reset-password/')).toBe(true);
    expect(isPublicRoutePath('/setup')).toBe(true);
  });

  it('keeps application pages protected', () => {
    expect(isPublicRoutePath('/')).toBe(false);
    expect(isPublicRoutePath('/projects')).toBe(false);
    expect(isPublicRoutePath('/reset-password/extra')).toBe(false);
    expect(isPublicRoutePath(null)).toBe(false);
  });
});
