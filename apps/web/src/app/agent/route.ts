import { backendApiBase } from '@/lib/backend-url';

/**
 * `/agent`: the setup steps an AI coding agent follows, as markdown.
 *
 * The web app answers the short public URL people paste into their agent,
 * so it works behind any ingress that sends `/` to the frontend. The API
 * owns the content (`/api/setup/agent.md`); this route only relays it.
 */
export async function GET(): Promise<Response> {
  const upstream = await fetch(`${backendApiBase()}/setup/agent.md`, { cache: 'no-store' });
  return new Response(await upstream.text(), {
    status: upstream.status,
    headers: {
      'content-type': upstream.headers.get('content-type') ?? 'text/markdown; charset=utf-8',
    },
  });
}
