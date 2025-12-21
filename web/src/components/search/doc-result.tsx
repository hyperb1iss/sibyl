'use client';

import Link from 'next/link';
import type { RAGChunkResult, RAGPageResult } from '@/lib/api';

interface DocChunkResultProps {
  result: RAGChunkResult;
}

/**
 * Display a documentation chunk search result.
 * Shows source, heading path, and snippet with similarity score.
 */
export function DocChunkResult({ result }: DocChunkResultProps) {
  const scorePercent = Math.round(result.similarity * 100);

  return (
    <a
      href={result.url}
      target="_blank"
      rel="noopener noreferrer"
      className="block bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-4 transition-all duration-200 hover:shadow-lg hover:border-sc-cyan/30"
    >
      <div className="space-y-2">
        {/* Header: Source + Score */}
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 min-w-0">
            <span className="shrink-0 px-2 py-0.5 text-xs font-medium rounded bg-sc-cyan/10 text-sc-cyan border border-sc-cyan/20">
              {result.source_name}
            </span>
            <span className="text-xs text-sc-fg-subtle shrink-0">
              {result.chunk_type}
            </span>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <div className="w-16 h-1.5 bg-sc-bg-elevated rounded-full overflow-hidden">
              <div
                className="h-full bg-sc-cyan rounded-full transition-all"
                style={{ width: `${scorePercent}%` }}
              />
            </div>
            <span className="text-xs text-sc-fg-muted">{scorePercent}%</span>
          </div>
        </div>

        {/* Heading Path (breadcrumb) */}
        {result.heading_path.length > 0 && (
          <div className="flex items-center gap-1 text-xs text-sc-fg-muted overflow-x-auto">
            {result.heading_path.map((heading, i) => (
              <span key={i} className="flex items-center gap-1 shrink-0">
                {i > 0 && <span className="text-sc-fg-subtle">/</span>}
                <span className="truncate max-w-[150px]">{heading}</span>
              </span>
            ))}
          </div>
        )}

        {/* Title */}
        <h3 className="text-base font-semibold text-sc-fg-primary line-clamp-1">
          {result.title}
        </h3>

        {/* Content Preview */}
        <p className="text-sc-fg-muted text-sm line-clamp-3 leading-relaxed">
          {result.content}
        </p>

        {/* Footer: URL */}
        <div className="pt-1">
          <span className="text-xs text-sc-fg-subtle truncate block">
            {result.url}
          </span>
        </div>
      </div>
    </a>
  );
}

interface DocPageResultProps {
  result: RAGPageResult;
}

/**
 * Display a documentation page search result.
 * Shows full page with metadata.
 */
export function DocPageResult({ result }: DocPageResultProps) {
  const scorePercent = Math.round(result.best_chunk_similarity * 100);

  return (
    <a
      href={result.url}
      target="_blank"
      rel="noopener noreferrer"
      className="block bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-4 transition-all duration-200 hover:shadow-lg hover:border-sc-cyan/30"
    >
      <div className="space-y-2">
        {/* Header: Source + Score */}
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 min-w-0">
            <span className="shrink-0 px-2 py-0.5 text-xs font-medium rounded bg-sc-cyan/10 text-sc-cyan border border-sc-cyan/20">
              {result.source_name}
            </span>
            {result.has_code && (
              <span className="shrink-0 px-2 py-0.5 text-xs font-medium rounded bg-sc-purple/10 text-sc-purple border border-sc-purple/20">
                has code
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <div className="w-16 h-1.5 bg-sc-bg-elevated rounded-full overflow-hidden">
              <div
                className="h-full bg-sc-cyan rounded-full transition-all"
                style={{ width: `${scorePercent}%` }}
              />
            </div>
            <span className="text-xs text-sc-fg-muted">{scorePercent}%</span>
          </div>
        </div>

        {/* Title */}
        <h3 className="text-base font-semibold text-sc-fg-primary line-clamp-1">
          {result.title}
        </h3>

        {/* Headings Preview */}
        {result.headings.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {result.headings.slice(0, 5).map((heading, i) => (
              <span
                key={i}
                className="px-1.5 py-0.5 text-xs bg-sc-bg-elevated text-sc-fg-muted rounded"
              >
                {heading}
              </span>
            ))}
            {result.headings.length > 5 && (
              <span className="px-1.5 py-0.5 text-xs text-sc-fg-subtle">
                +{result.headings.length - 5} more
              </span>
            )}
          </div>
        )}

        {/* Content Preview */}
        <p className="text-sc-fg-muted text-sm line-clamp-2 leading-relaxed">
          {result.content.slice(0, 300)}...
        </p>

        {/* Footer: Stats + URL */}
        <div className="flex items-center justify-between pt-1">
          <div className="flex items-center gap-3 text-xs text-sc-fg-subtle">
            <span>{result.word_count.toLocaleString()} words</span>
            {result.code_languages.length > 0 && (
              <span>{result.code_languages.join(', ')}</span>
            )}
          </div>
          <span className="text-xs text-sc-fg-subtle truncate max-w-[200px]">
            {new URL(result.url).hostname}
          </span>
        </div>
      </div>
    </a>
  );
}

/**
 * Determine if a result is a chunk or page result.
 */
export function isChunkResult(
  result: RAGChunkResult | RAGPageResult
): result is RAGChunkResult {
  return 'chunk_id' in result;
}

interface DocResultProps {
  result: RAGChunkResult | RAGPageResult;
}

/**
 * Auto-detect and render the appropriate result component.
 */
export function DocResult({ result }: DocResultProps) {
  if (isChunkResult(result)) {
    return <DocChunkResult result={result} />;
  }
  return <DocPageResult result={result} />;
}
