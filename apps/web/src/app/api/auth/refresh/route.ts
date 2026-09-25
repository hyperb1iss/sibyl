import type { NextRequest } from 'next/server';
import { backendApiBase } from '@/lib/backend-url';

function setCookieHeaders(headers: Headers): string[] {
  const withGetSetCookie = headers as Headers & { getSetCookie?: () => string[] };
  const values = withGetSetCookie.getSetCookie?.();
  if (values?.length) return values;

  const single = headers.get('set-cookie');
  return single ? [single] : [];
}

function copyRefreshHeaders(from: Response, to: Response): void {
  for (const cookie of setCookieHeaders(from.headers)) {
    to.headers.append('set-cookie', cookie);
  }

  const retryAfter = from.headers.get('retry-after');
  if (retryAfter) {
    to.headers.set('retry-after', retryAfter);
  }
}

async function requestRefresh(request: Request, body?: BodyInit | null): Promise<Response> {
  const headers = new Headers();
  const cookie = request.headers.get('cookie');
  if (cookie) headers.set('cookie', cookie);

  const contentType = request.headers.get('content-type');
  if (contentType && body) headers.set('content-type', contentType);

  return fetch(`${backendApiBase()}/auth/refresh`, {
    method: 'POST',
    headers,
    body,
    cache: 'no-store',
    redirect: 'manual',
  });
}

export async function POST(request: NextRequest): Promise<Response> {
  const body = await request.text();
  const backendResponse = await requestRefresh(request, body || null);
  const headers = new Headers();

  const contentType = backendResponse.headers.get('content-type');
  if (contentType) {
    headers.set('content-type', contentType);
  }

  const response = new Response(await backendResponse.arrayBuffer(), {
    status: backendResponse.status,
    headers,
  });
  copyRefreshHeaders(backendResponse, response);
  return response;
}
