import { NextRequest } from 'next/server';
import { describe, expect, it } from 'vitest';
import { proxy } from './proxy';

describe('proxy auth refresh', () => {
  it('allows protected page loads when only the refresh cookie is usable', () => {
    const request = new NextRequest('http://web.test/projects?view=active', {
      headers: {
        cookie: 'sibyl_refresh_token=refresh',
      },
    });

    const response = proxy(request);

    expect(response.status).toBe(200);
    expect(response.headers.get('location')).toBeNull();
  });

  it('redirects protected page loads without auth cookies to login', () => {
    const request = new NextRequest('http://web.test/projects?view=active');

    const response = proxy(request);

    expect(response.status).toBe(307);
    expect(response.headers.get('location')).toBe(
      'http://web.test/login?next=%2Fprojects%3Fview%3Dactive'
    );
  });

  it('keeps a Net thread deep link whole through the login redirect', () => {
    const deepLink = '/net?channel=%40ada&thread=gh%3Aacme%2Fapp%2342';
    const response = proxy(new NextRequest(`http://web.test${deepLink}`));

    const location = new URL(response.headers.get('location') ?? '');
    expect(location.pathname).toBe('/login');
    expect(location.searchParams.get('next')).toBe(deepLink);
    // And the page reads it back as the same thread.
    const next = new URL(location.searchParams.get('next') ?? '', 'http://web.test');
    expect(next.searchParams.get('channel')).toBe('@ada');
    expect(next.searchParams.get('thread')).toBe('gh:acme/app#42');
  });

  it('does not refresh public login traffic', () => {
    const request = new NextRequest('http://web.test/login', {
      headers: { cookie: 'sibyl_refresh_token=refresh' },
    });

    const response = proxy(request);

    expect(response.status).toBe(200);
    expect(response.headers.get('location')).toBeNull();
  });

  it('allows password reset links without auth cookies', () => {
    const request = new NextRequest('http://web.test/reset-password?token=reset-token');

    const response = proxy(request);

    expect(response.status).toBe(200);
    expect(response.headers.get('location')).toBeNull();
  });
});
