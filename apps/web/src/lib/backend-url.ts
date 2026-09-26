/**
 * Server-side base URL of the Sibyl REST API, for route handlers that proxy
 * to the backend. The Helm chart sets SIBYL_API_URL to the in-cluster
 * service; local dev falls back to the API port.
 */
export function backendApiBase(): string {
  const explicit = process.env.SIBYL_API_URL;
  if (explicit) return explicit.replace(/\/$/, '');

  const backend = process.env.SIBYL_BACKEND_URL || 'http://127.0.0.1:3334';
  return `${backend.replace(/\/$/, '')}/api`;
}
