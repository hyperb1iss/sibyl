import type { NextRequest } from 'next/server';
import { NextResponse } from 'next/server';

import {
  ACCESS_TOKEN_COOKIE,
  REFRESH_TOKEN_COOKIE,
  safeRelativePath,
  shouldRefreshAuthCookies,
} from '@/lib/auth-refresh';
import { log } from '@/lib/logger';
import { isPublicRoutePath } from '@/lib/public-routes';

/**
 * Check if request has valid auth cookies.
 * We accept EITHER access token OR refresh token.
 *
 * If only refresh token is present (access token expired), the page will load
 * and the frontend will attempt a token refresh on the first API call.
 *
 * We don't validate tokens here - that happens in API routes.
 * This just gates access to protected pages.
 */
function hasAuthCookie(request: NextRequest): boolean {
  const hasAccess = !!request.cookies.get(ACCESS_TOKEN_COOKIE)?.value;
  const hasRefresh = !!request.cookies.get(REFRESH_TOKEN_COOKIE)?.value;
  return hasAccess || hasRefresh;
}

function refreshResponse(request: NextRequest): NextResponse {
  const { pathname, search } = request.nextUrl;
  const url = new URL('/api/auth/refresh', request.url);
  url.searchParams.set('next', safeRelativePath(`${pathname}${search}`));
  return NextResponse.redirect(url);
}

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const start = Date.now();

  // Login, setup, and password reset pages: always allow without auth
  // Setup page handles its own redirect if setup is already complete
  if (isPublicRoutePath(pathname)) {
    log.debug('proxy', { path: pathname, action: 'allow_public' });
    return NextResponse.next();
  }

  // All other pages: require auth cookie
  if (!hasAuthCookie(request)) {
    const url = request.nextUrl.clone();
    url.pathname = '/login';
    url.searchParams.set('next', `${pathname}${search}`);
    log.info('proxy', { path: pathname, action: 'redirect_login', ms: Date.now() - start });
    return NextResponse.redirect(url);
  }

  const accessToken = request.cookies.get(ACCESS_TOKEN_COOKIE)?.value;
  const refreshToken = request.cookies.get(REFRESH_TOKEN_COOKIE)?.value;
  if (shouldRefreshAuthCookies({ accessToken, refreshToken })) {
    log.info('proxy', { path: pathname, action: 'refresh_auth', ms: Date.now() - start });
    return refreshResponse(request);
  }

  log.debug('proxy', { path: pathname, action: 'allow', ms: Date.now() - start });
  return NextResponse.next();
}

export const config = {
  matcher: [
    // Exclude API routes, static files, image optimizations, and common assets.
    '/((?!api|_next/static|_next/image|favicon.ico|.*\\..*).*)',
  ],
};
