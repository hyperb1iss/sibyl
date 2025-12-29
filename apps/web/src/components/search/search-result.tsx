import Link from 'next/link';
import { EntityBadge } from '@/components/ui/badge';
import { ExternalLink } from '@/components/ui/icons';
import { ENTITY_ICONS, type EntityType, getEntityStyles } from '@/lib/constants';

interface SearchResult {
  id: string;
  type: string;
  name: string;
  content?: string | null;
  score: number;
  url?: string | null;
  result_origin?: 'graph' | 'document';
  metadata?: Record<string, unknown>;
}

interface SearchResultCardProps {
  result: SearchResult;
}

export function SearchResultCard({ result }: SearchResultCardProps) {
  const styles = getEntityStyles(result.type);
  const icon = ENTITY_ICONS[result.type as EntityType] ?? '◇';
  const scorePercent = Math.round(result.score * 100);

  // Determine the link based on result type
  // Documents with source_id/document_id link to the document viewer
  const documentId = result.metadata?.document_id as string | undefined;
  const sourceId = result.metadata?.source_id as string | undefined;
  const externalUrl = result.url;

  const isDocument = result.result_origin === 'document' && sourceId && documentId;
  const href = isDocument
    ? `/sources/${sourceId}/documents/${documentId}`
    : `/entities/${result.id}`;

  return (
    <Link
      href={href}
      className={`
        relative block overflow-hidden rounded-xl
        bg-gradient-to-br ${styles.gradient}
        border ${styles.border}
        transition-all duration-200 group
        hover:shadow-lg ${styles.glow}
        hover:-translate-y-0.5
      `}
    >
      {/* Accent bar */}
      <div className={`absolute left-0 top-0 bottom-0 w-1 ${styles.accent}`} />

      <div className="pl-4 pr-3 py-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            {/* Header: Icon + Badge + Score */}
            <div className="flex items-center gap-2 mb-2.5">
              <span className={`text-lg ${styles.dot.replace('bg-', 'text-')}`}>{icon}</span>
              <EntityBadge type={result.type} />
              <span className="ml-auto text-xs text-sc-fg-subtle flex items-center gap-1">
                <span className={`inline-block w-1.5 h-1.5 rounded-full ${styles.dot}`} />
                {scorePercent}%
              </span>
            </div>

            {/* Title */}
            <h3 className="text-base font-semibold text-sc-fg-primary truncate transition-colors group-hover:text-white">
              {result.name}
            </h3>

            {/* Content preview */}
            {result.content && (
              <p className="text-sc-fg-muted text-sm mt-1.5 line-clamp-2 leading-relaxed">
                {result.content}
              </p>
            )}

            {/* External URL for documents */}
            {externalUrl && (
              <div className="flex items-center justify-between mt-2 pt-2 border-t border-sc-fg-subtle/10">
                <span className="text-xs text-sc-fg-subtle truncate max-w-[250px]">
                  {externalUrl}
                </span>
                <button
                  type="button"
                  onClick={e => {
                    e.preventDefault();
                    e.stopPropagation();
                    window.open(externalUrl, '_blank', 'noopener,noreferrer');
                  }}
                  className="shrink-0 p-1 text-sc-fg-subtle hover:text-sc-cyan transition-colors"
                  title="Open original page"
                >
                  <ExternalLink width={14} height={14} />
                </button>
              </div>
            )}
          </div>

          {/* Score indicator */}
          <div className="shrink-0 flex flex-col items-end gap-1 pt-1">
            <div className="w-20 h-1.5 bg-sc-bg-elevated rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full transition-all ${styles.accent}`}
                style={{ width: `${scorePercent}%` }}
              />
            </div>
          </div>
        </div>
      </div>
    </Link>
  );
}

export function SearchResultSkeleton() {
  return (
    <div className="relative bg-sc-bg-base rounded-xl overflow-hidden border border-sc-fg-subtle/10 animate-pulse">
      <div className="absolute left-0 top-0 bottom-0 w-1 bg-sc-fg-subtle/20" />
      <div className="pl-4 pr-3 py-4">
        <div className="flex items-center gap-2 mb-2.5">
          <div className="w-5 h-5 bg-sc-bg-elevated rounded" />
          <div className="h-5 w-16 bg-sc-bg-elevated rounded" />
          <div className="ml-auto h-4 w-10 bg-sc-bg-elevated rounded" />
        </div>
        <div className="h-5 w-3/4 bg-sc-bg-elevated rounded mb-2" />
        <div className="h-4 w-full bg-sc-bg-elevated rounded" />
      </div>
    </div>
  );
}
