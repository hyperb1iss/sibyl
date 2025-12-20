import Link from 'next/link';
import { EntityBadge } from '@/components/ui/badge';
import { ScoreBar } from '@/components/ui/progress';
import { getEntityStyles } from '@/lib/constants';

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
  const scorePercent = Math.round(result.score * 100);

  return (
    <Link
      href={`/entities/${result.id}`}
      className={`
        block bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-4
        transition-all duration-200
        hover:shadow-lg
        ${styles.card}
      `}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-2">
            <EntityBadge type={result.type} />
            <span className="text-xs text-sc-fg-subtle">{scorePercent}% match</span>
          </div>
          <h3 className="text-lg font-semibold text-sc-fg-primary truncate">
            {result.name}
          </h3>
          {result.content && (
            <p className="text-sc-fg-muted text-sm mt-1 line-clamp-2">
              {result.content}
            </p>
          )}
        </div>
        <div className="flex-shrink-0 pt-1">
          <ScoreBar score={result.score} />
        </div>
      </div>
    </Link>
  );
}
