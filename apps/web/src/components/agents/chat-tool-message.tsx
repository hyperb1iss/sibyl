'use client';

/**
 * Tool message component - displays tool calls with expandable results.
 */

import { useMemo, useRef, useState } from 'react';
import { ChevronDown } from '@/components/ui/icons';
import { getToolIcon, getToolStatus, stripAnsi } from './chat-constants';
import type { ToolMessageProps } from './chat-types';
import { ToolContentRenderer } from './tool-renderers';

// =============================================================================
// ToolMessage
// =============================================================================

/** Collapsible tool execution display with result preview */
export function ToolMessage({ message, result, isNew = false, tier3Hint }: ToolMessageProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);

  const iconName = message.metadata?.icon as string | undefined;
  const toolName = message.metadata?.tool_name as string | undefined;
  const Icon = getToolIcon(iconName);
  const input = message.metadata?.input as Record<string, unknown> | undefined;

  // Memoize Tier 2 playful status so it doesn't change on re-render
  const playfulStatus = useMemo(
    () => (toolName ? getToolStatus(toolName, input) : null),
    [toolName, input]
  );

  // Tier 3 hint overrides Tier 2 when available
  const displayHint = tier3Hint || playfulStatus;

  // For results, check error status
  const resultError = result?.metadata?.is_error as boolean | undefined;
  const hasResult = !!result;

  // Get result preview (short, strip ANSI codes)
  const getResultPreview = () => {
    if (!result) return null;
    const content = stripAnsi(result.content);
    const firstLine = content.split('\n')[0] || '';
    // For file counts, errors, etc - show brief
    if (firstLine.length < 60) return firstLine;
    return `${firstLine.slice(0, 50)}...`;
  };

  const resultPreview = getResultPreview();
  // Always expandable if there's a result (user might want to see full output)
  // Also expandable for long content
  const resultContentLength = result?.content?.length ?? 0;
  const hasExpandableContent =
    hasResult || message.content.length > 100 || resultContentLength > 100;

  // Status indicator color
  const statusClass = !hasResult
    ? 'text-sc-purple' // Pending
    : resultError
      ? 'text-sc-red' // Error
      : 'text-sc-green'; // Success

  return (
    <div
      className={`rounded-md font-mono text-xs overflow-hidden transition-all duration-300 ${isNew ? 'animate-slide-up' : ''} ${
        isExpanded
          ? 'bg-sc-bg-elevated ring-1 ring-sc-purple/30 shadow-lg shadow-sc-purple/5'
          : 'bg-sc-bg-elevated/50 hover:bg-sc-bg-elevated/80'
      }`}
    >
      {/* Header row with tool name and preview */}
      <button
        type="button"
        onClick={() => hasExpandableContent && setIsExpanded(!isExpanded)}
        className={`w-full flex items-center gap-1.5 px-2 py-1.5 text-left transition-all duration-200 group ${
          hasExpandableContent ? 'cursor-pointer hover:bg-sc-fg-subtle/5' : ''
        }`}
      >
        <Icon
          width={12}
          height={12}
          className={`${statusClass} shrink-0 transition-all duration-200 ${isExpanded ? 'scale-110' : ''} ${!hasResult ? 'animate-pulse' : ''}`}
        />
        <span className={`${statusClass} font-medium shrink-0 transition-colors duration-200`}>
          {toolName || 'Tool'}
        </span>
        <span className="text-sc-fg-muted truncate flex-1 min-w-0 group-hover:text-sc-fg-primary transition-colors duration-200">
          {/* Show contextual hint when pending (Tier 3 > Tier 2), raw content when complete */}
          {!hasResult && displayHint ? (
            <span className="italic">{displayHint}</span>
          ) : (
            message.content
          )}
        </span>
        {/* Result indicator with entrance animation */}
        {hasResult && (
          <span className={`text-[10px] shrink-0 ${statusClass} animate-fade-in`}>
            {resultError ? '' : ''} {resultPreview?.slice(0, 20)}
            {resultPreview && resultPreview.length > 20 ? '...' : ''}
          </span>
        )}
        <span className="text-[10px] text-sc-fg-subtle shrink-0 tabular-nums ml-1">
          {message.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
        </span>
        {hasExpandableContent && (
          <ChevronDown
            width={12}
            height={12}
            className={`text-sc-fg-subtle shrink-0 transition-transform duration-300 ${isExpanded ? 'rotate-180' : ''}`}
          />
        )}
      </button>

      {/* Animated expandable content */}
      <div
        ref={contentRef}
        className={`overflow-hidden transition-all duration-300 ease-out ${
          isExpanded ? 'max-h-[500px] opacity-100' : 'max-h-0 opacity-0'
        }`}
      >
        <div className="border-t border-sc-fg-subtle/10 p-2">
          {/* Use smart renderer for code tools, fallback to simple display */}
          {toolName && ['Read', 'Edit', 'Write', 'Bash', 'Grep', 'Glob'].includes(toolName) ? (
            <ToolContentRenderer
              toolName={toolName}
              input={message.metadata?.input as Record<string, unknown> | undefined}
              result={(result?.metadata?.full_content as string | undefined) || result?.content}
              isError={resultError}
            />
          ) : result ? (
            <pre
              className={`whitespace-pre-wrap break-words text-[11px] leading-relaxed p-2 rounded ${
                resultError ? 'bg-sc-red/5 text-sc-red' : 'bg-sc-bg-dark text-sc-fg-primary'
              }`}
            >
              {(result.metadata?.full_content as string | undefined) || result.content}
            </pre>
          ) : (
            <pre className="whitespace-pre-wrap break-words text-[11px] leading-relaxed p-2 rounded bg-sc-bg-dark text-sc-fg-primary">
              {message.content}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}
