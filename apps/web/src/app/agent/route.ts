import { backendApiBase } from '@/lib/backend-url';

const UPSTREAM_TIMEOUT_MS = 5000;

/**
 * `/agent`: the setup steps an AI coding agent follows, as markdown.
 *
 * The web app answers the short public URL people paste into their agent,
 * so it works behind any ingress that sends `/` to the frontend. The API
 * owns the content (`/api/setup/agent.md`); this route only relays it.
 */
export async function GET(): Promise<Response> {
  try {
    const upstream = await fetch(`${backendApiBase()}/setup/agent.md`, {
      cache: 'no-store',
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
    return new Response(await upstream.text(), {
      status: upstream.status,
      headers: {
        'content-type': upstream.headers.get('content-type') ?? 'text/markdown; charset=utf-8',
      },
    });
  } catch {
    // An agent reads this body, so say what failed in words it can relay.
    return new Response(
      '# Sibyl setup is unavailable\n\nThe Sibyl server did not answer. Try again in a minute.\n',
      { status: 502, headers: { 'content-type': 'text/markdown; charset=utf-8' } }
    );
  }
}
