import { formatDateTime } from '@/lib/constants/formatting';

interface MetadataFieldsProps {
  metadata: Record<string, unknown>;
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

function isScalar(value: unknown): value is string | number | boolean {
  return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean';
}

function formatScalar(value: string | number | boolean): string {
  if (typeof value === 'string' && ISO_DATE.test(value)) {
    const parsed = new Date(value);
    if (!Number.isNaN(parsed.getTime())) return formatDateTime(parsed);
  }
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  return String(value);
}

function labelFor(key: string): string {
  return key.replace(/^_+/, '').replace(/[_-]+/g, ' ');
}

/**
 * Entity metadata as readable fields instead of a JSON dump. Scalars render
 * as label/value rows (dates humanized), nested values render compact, and
 * the verbatim JSON stays one click away for anyone who needs the exact
 * bytes. Keys prefixed with an underscore are internal bookkeeping and sort
 * to the end.
 */
export function MetadataFields({ metadata }: MetadataFieldsProps) {
  const keys = Object.keys(metadata).sort((a, b) => {
    const aInternal = a.startsWith('_');
    const bInternal = b.startsWith('_');
    if (aInternal !== bInternal) return aInternal ? 1 : -1;
    return a.localeCompare(b);
  });

  if (keys.length === 0) return null;

  return (
    <div className="space-y-3">
      <dl className="space-y-2.5">
        {keys.map(key => {
          const value = metadata[key];
          const isInternal = key.startsWith('_');
          return (
            <div key={key} className="min-w-0">
              <dt
                className={`text-xs uppercase tracking-wide ${
                  isInternal ? 'text-sc-fg-subtle/70' : 'text-sc-fg-subtle'
                }`}
              >
                {labelFor(key)}
              </dt>
              <dd className="mt-0.5 text-sm text-sc-fg-muted break-words">
                {value === null || value === undefined ? (
                  <span className="italic text-sc-fg-subtle">none</span>
                ) : isScalar(value) ? (
                  <span
                    className={typeof value === 'string' && value.length > 48 ? 'break-all' : ''}
                  >
                    {formatScalar(value)}
                  </span>
                ) : (
                  <code className="block max-h-32 overflow-auto rounded-md border border-sc-fg-subtle/15 bg-sc-bg-highlight px-2 py-1 font-mono text-xs whitespace-pre-wrap">
                    {JSON.stringify(value, null, 1)}
                  </code>
                )}
              </dd>
            </div>
          );
        })}
      </dl>
      <details className="group">
        <summary className="cursor-pointer text-xs text-sc-fg-subtle transition-colors hover:text-sc-cyan focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan rounded">
          Raw JSON
        </summary>
        <pre className="mt-2 rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight p-3 font-mono text-xs text-sc-fg-muted whitespace-pre-wrap overflow-x-auto">
          {JSON.stringify(metadata, null, 2)}
        </pre>
      </details>
    </div>
  );
}
