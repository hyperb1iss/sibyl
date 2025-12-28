import type { NextRequest } from 'next/server';
import { NextResponse } from 'next/server';

const ACCESS_TOKEN_COOKIE = 'sibyl_access_token';
const REFRESH_TOKEN_COOKIE = 'sibyl_refresh_token';

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

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;

  // Login page: redirect to home if already has token
  if (pathname === '/login') {
    if (hasAuthCookie(request)) {
      return NextResponse.redirect(new URL('/', request.url));
    }
    return NextResponse.next();
  }

  // All other pages: require auth cookie
  if (!hasAuthCookie(request)) {
    const url = request.nextUrl.clone();
    url.pathname = '/login';
    url.searchParams.set('next', `${pathname}${search}`);
    return NextResponse.redirect(url);
  }

  return NextResponse.next();
}

export const config = {
  matcher: [
    // Exclude API routes, static files, image optimizations, and common assets.
    '/((?!api|_next/static|_next/image|favicon.ico|.*\\..*).*)',
  ],
};
