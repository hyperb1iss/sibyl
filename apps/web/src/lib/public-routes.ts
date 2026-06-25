const PUBLIC_ROUTE_PATHS = new Set(['/login', '/reset-password', '/setup']);

export function isPublicRoutePath(pathname: string | null | undefined): boolean {
  if (!pathname) return false;
  const normalized =
    pathname.length > 1 && pathname.endsWith('/') ? pathname.slice(0, -1) : pathname;
  return PUBLIC_ROUTE_PATHS.has(normalized);
}
