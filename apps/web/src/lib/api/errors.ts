/**
 * The body the API sends for an HTTP error, as `fetchApi` surfaces it: the
 * thrown Error's message is the raw response text.
 */
export interface ApiErrorBody {
  error?: string;
  message?: string;
  remediation?: string;
  request_id?: string;
  details?: Record<string, unknown>;
}

/** A deployment-owned setting named by a `locked_by_env` refusal. */
export interface LockedSetting {
  field: string;
  env_var?: string;
}

export function parseApiError(error: unknown): ApiErrorBody | null {
  if (!(error instanceof Error)) return null;
  try {
    const body = JSON.parse(error.message) as unknown;
    return body && typeof body === 'object' ? (body as ApiErrorBody) : null;
  } catch {
    return null;
  }
}

function lockedSettings(value: unknown): LockedSetting[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (entry): entry is LockedSetting =>
      Boolean(entry) && typeof entry === 'object' && typeof entry.field === 'string'
  );
}

/**
 * The settings a 409 `locked_by_env` refusal names: `fields` are the ones the
 * request tried to change, `deploymentOwned` every one the deployment owns.
 * Null when the error is anything else.
 */
export function parseLockedByEnv(
  error: unknown
): { fields: LockedSetting[]; deploymentOwned: LockedSetting[] } | null {
  const body = parseApiError(error);
  if (body?.error !== 'locked_by_env') return null;
  return {
    fields: lockedSettings(body.details?.fields),
    deploymentOwned: lockedSettings(body.details?.deployment_owned),
  };
}
