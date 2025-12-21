'use client';

import type { CodeExampleResult } from '@/lib/api';

// Language colors for common languages
const LANGUAGE_COLORS: Record<string, string> = {
  python: 'bg-[#3572A5]/20 text-[#3572A5] border-[#3572A5]/30',
  typescript: 'bg-[#3178c6]/20 text-[#3178c6] border-[#3178c6]/30',
  javascript: 'bg-[#f1e05a]/20 text-[#f1e05a] border-[#f1e05a]/30',
  rust: 'bg-[#dea584]/20 text-[#dea584] border-[#dea584]/30',
  go: 'bg-[#00ADD8]/20 text-[#00ADD8] border-[#00ADD8]/30',
  java: 'bg-[#b07219]/20 text-[#b07219] border-[#b07219]/30',
  ruby: 'bg-[#701516]/20 text-[#cc342d] border-[#701516]/30',
  php: 'bg-[#4F5D95]/20 text-[#4F5D95] border-[#4F5D95]/30',
  css: 'bg-[#563d7c]/20 text-[#563d7c] border-[#563d7c]/30',
  html: 'bg-[#e34c26]/20 text-[#e34c26] border-[#e34c26]/30',
  bash: 'bg-sc-fg-subtle/20 text-sc-fg-muted border-sc-fg-subtle/30',
  shell: 'bg-sc-fg-subtle/20 text-sc-fg-muted border-sc-fg-subtle/30',
  sql: 'bg-[#e38c00]/20 text-[#e38c00] border-[#e38c00]/30',
  json: 'bg-sc-yellow/20 text-sc-yellow border-sc-yellow/30',
  yaml: 'bg-sc-coral/20 text-sc-coral border-sc-coral/30',
  markdown: 'bg-sc-cyan/20 text-sc-cyan border-sc-cyan/30',
};

interface CodeResultProps {
  result: CodeExampleResult;
}

/**
 * Display a code example search result.
 * Shows syntax highlighted code with context.
 */
export function CodeResult({ result }: CodeResultProps) {
  const scorePercent = Math.round(result.similarity * 100);
  const language = result.language?.toLowerCase() || 'code';
  const langColor =
    LANGUAGE_COLORS[language] || 'bg-sc-purple/20 text-sc-purple border-sc-purple/30';

  // Truncate code for display (first ~15 lines)
  const codeLines = result.code.split('\n');
  const displayCode = codeLines.slice(0, 15).join('\n');
  const hasMore = codeLines.length > 15;

  return (
    <a
      href={result.url}
      target="_blank"
      rel="noopener noreferrer"
      className="block bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl overflow-hidden transition-all duration-200 hover:shadow-lg hover:border-sc-purple/30"
    >
      {/* Header */}
      <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-sc-fg-subtle/10">
        <div className="flex items-center gap-2 min-w-0">
          <span
            className={`shrink-0 px-2 py-0.5 text-xs font-medium rounded border ${langColor}`}
          >
            {result.language || 'code'}
          </span>
          <span className="text-xs text-sc-fg-subtle truncate">
            {result.source_name}
          </span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <div className="w-16 h-1.5 bg-sc-bg-elevated rounded-full overflow-hidden">
            <div
              className="h-full bg-sc-purple rounded-full transition-all"
              style={{ width: `${scorePercent}%` }}
            />
          </div>
          <span className="text-xs text-sc-fg-muted">{scorePercent}%</span>
        </div>
      </div>

      {/* Heading Path */}
      {result.heading_path.length > 0 && (
        <div className="flex items-center gap-1 px-4 py-2 text-xs text-sc-fg-muted bg-sc-bg-elevated/50 overflow-x-auto">
          {result.heading_path.map((heading, i) => (
            <span key={i} className="flex items-center gap-1 shrink-0">
              {i > 0 && <span className="text-sc-fg-subtle">/</span>}
              <span className="truncate max-w-[150px]">{heading}</span>
            </span>
          ))}
        </div>
      )}

      {/* Code Block */}
      <div className="relative">
        <pre className="p-4 text-sm overflow-x-auto bg-sc-bg-dark">
          <code className="font-mono text-sc-fg-primary leading-relaxed whitespace-pre">
            {displayCode}
          </code>
        </pre>
        {hasMore && (
          <div className="absolute bottom-0 left-0 right-0 h-12 bg-gradient-to-t from-sc-bg-dark to-transparent flex items-end justify-center pb-2">
            <span className="text-xs text-sc-fg-subtle px-2 py-1 bg-sc-bg-elevated rounded">
              +{codeLines.length - 15} more lines
            </span>
          </div>
        )}
      </div>

      {/* Context (if available) */}
      {result.context && (
        <div className="px-4 py-2 border-t border-sc-fg-subtle/10 bg-sc-bg-elevated/30">
          <p className="text-xs text-sc-fg-muted line-clamp-2">{result.context}</p>
        </div>
      )}

      {/* Footer */}
      <div className="flex items-center justify-between px-4 py-2 border-t border-sc-fg-subtle/10">
        <span className="text-sm font-medium text-sc-fg-primary truncate">
          {result.title}
        </span>
        <span className="text-xs text-sc-fg-subtle shrink-0">
          {new URL(result.url).hostname}
        </span>
      </div>
    </a>
  );
}

interface CodeResultListProps {
  results: CodeExampleResult[];
}

/**
 * Display a list of code example results.
 */
export function CodeResultList({ results }: CodeResultListProps) {
  if (results.length === 0) {
    return null;
  }

  return (
    <div className="space-y-3">
      {results.map(result => (
        <CodeResult key={result.chunk_id} result={result} />
      ))}
    </div>
  );
}
