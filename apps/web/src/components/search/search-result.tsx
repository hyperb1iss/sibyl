import Link from 'next/link';
import { EntityBadge } from '@/components/ui/badge';
import { ENTITY_ICONS, type EntityType, getEntityStyles } from '@/lib/constants';

interface SearchResult {
  id: string;
  type: string;
  name: string;
  content?: string | null;
  score: number;
}

interface SearchResultCardProps {
  result: SearchResult;
}

export function SearchResultCard({ result }: SearchResultCardProps) {
  const styles = getEntityStyles(result.type);
  const icon = ENTITY_ICONS[result.type as EntityType] ?? '◇';
  const scorePercent = Math.round(result.score * 100);

  return (
    <Link
      href={`/entities/${result.id}`}
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
