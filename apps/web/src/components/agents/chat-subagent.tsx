'use client';

/**
 * Subagent execution display components.
 */

import { useEffect, useRef, useState } from 'react';
import { Check, ChevronDown, Code, Xmark } from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import { formatDuration, getToolIcon } from './chat-constants';
import { ToolMessage } from './chat-tool-message';
import type { ChatMessage, ParallelAgentsBlockProps, SubagentBlockProps } from './chat-types';

// =============================================================================
// SubagentBlock
// =============================================================================

/** Renders a single subagent task with nested tool calls */
export function SubagentBlock({
  taskCall,
  taskResult,
  nestedCalls,
  resultsByToolId,
  pollingCalls = [],
}: SubagentBlockProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);

  // Extract metadata from task call
  const description = taskCall.content || 'Subagent task';
  const shortDescription = description.length > 50 ? `${description.slice(0, 47)}...` : description;
  const agentType = taskCall.metadata?.subagent_type as string | undefined;
  const isBackground = taskCall.metadata?.run_in_background as boolean | undefined;

  // Get latest poll status for background agents
  const latestPollResult =
    pollingCalls.length > 0
      ? resultsByToolId.get(pollingCalls[pollingCalls.length - 1].metadata?.tool_id as string)
      : undefined;
  const pollStatus = latestPollResult?.metadata?.status as
    | 'running'
    | 'completed'
    | 'failed'
    | undefined;
  const pollCount = pollingCalls.length;

  // Count tool calls
  const toolCount = nestedCalls.length;

  // Check status
  const hasResult = !!taskResult;
  const isWorking = !hasResult;
  const isError = taskResult?.metadata?.is_error as boolean | undefined;

  // Extract result metadata (cost, duration, etc.)
  const resultMeta = taskResult?.metadata as
    | { duration_ms?: number; total_cost_usd?: number }
    | undefined;
  const durationMs = resultMeta?.duration_ms;
  const costUsd = resultMeta?.total_cost_usd;

  // Get 2 most recent tool calls for live preview (only when working)
  const recentCalls = isWorking ? nestedCalls.slice(-2) : [];

  // Auto-scroll preview when new calls come in
  // biome-ignore lint/correctness/useExhaustiveDependencies: trigger on nested call count
  useEffect(() => {
    if (previewRef.current && isWorking) {
      previewRef.current.scrollTop = previewRef.current.scrollHeight;
    }
  }, [nestedCalls.length, isWorking]);

  return (
    <div
      className={`rounded-lg border overflow-hidden transition-all duration-300 animate-slide-up ${
        isWorking
          ? 'border-sc-purple/40 bg-gradient-to-r from-sc-purple/10 via-sc-cyan/5 to-sc-purple/10 shadow-lg shadow-sc-purple/10'
          : isError
            ? 'border-sc-red/30 bg-sc-red/5'
            : 'border-sc-green/20 bg-sc-bg-elevated/30'
      }`}
    >
      {/* Header */}
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-sc-fg-subtle/5 transition-all duration-200 group"
      >
        {/* Icon - use poll status for background agents */}
        <StatusIcon
          isBackground={isBackground}
          pollStatus={pollStatus}
          isWorking={isWorking}
          isError={isError}
        />

        {/* Agent type badge */}
        {agentType && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-sc-purple/20 text-sc-purple font-medium shrink-0 transition-transform duration-200 group-hover:scale-105">
            {agentType}
          </span>
        )}

        {/* Background indicator with poll count */}
        {isBackground && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-sc-cyan/20 text-sc-cyan shrink-0 flex items-center gap-1">
            bg
            {pollCount > 0 && <span className="text-sc-cyan/70">({pollCount}x)</span>}
          </span>
        )}

        <span className="text-xs text-sc-fg-muted truncate flex-1 min-w-0 group-hover:text-sc-fg-primary transition-colors duration-200">
          {shortDescription}
        </span>

        {/* Tool count with activity indicator */}
        {toolCount > 0 && (
          <span
            className={`text-[10px] text-sc-fg-subtle shrink-0 tabular-nums transition-colors duration-200 ${isWorking ? 'text-sc-purple' : ''}`}
          >
            {toolCount} tool{toolCount !== 1 ? 's' : ''}
          </span>
        )}

        {/* Status label */}
        <StatusLabel
          isBackground={isBackground}
          pollStatus={pollStatus}
          isWorking={isWorking}
          isError={isError}
        />

        {/* Duration (when complete) */}
        {durationMs && (
          <span className="text-[10px] text-sc-fg-subtle shrink-0 tabular-nums animate-fade-in">
            {formatDuration(durationMs)}
          </span>
        )}

        {/* Cost (when available) */}
        {costUsd && costUsd > 0 && (
          <span className="text-[10px] text-sc-coral shrink-0 tabular-nums animate-fade-in">
            ${costUsd.toFixed(4)}
          </span>
        )}

        {/* Timestamp */}
        <span className="text-[10px] text-sc-fg-subtle shrink-0 tabular-nums">
          {taskCall.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
        </span>

        <ChevronDown
          width={14}
          height={14}
          className={`text-sc-fg-subtle shrink-0 transition-transform duration-300 ${isExpanded ? 'rotate-180' : ''}`}
        />
      </button>

      {/* Live preview (only when working and not expanded) */}
      {!isExpanded && isWorking && recentCalls.length > 0 && (
        <LivePreview calls={recentCalls} resultsByToolId={resultsByToolId} ref={previewRef} />
      )}

      {/* Expanded content with animation */}
      <div
        ref={contentRef}
        className={`overflow-hidden transition-all duration-300 ease-in-out ${
          isExpanded ? 'max-h-[600px] opacity-100' : 'max-h-0 opacity-0'
        }`}
      >
        <div className="border-t border-sc-fg-subtle/10 bg-sc-bg-base/50 p-2 space-y-1 max-h-[500px] overflow-y-auto">
          {/* Show all nested tool calls */}
          {nestedCalls.length > 0 ? (
            nestedCalls.map(call => {
              const pairedResult = resultsByToolId.get(call.metadata?.tool_id as string);
              return <ToolMessage key={call.id} message={call} result={pairedResult} />;
            })
          ) : (
            <div className="text-xs text-sc-fg-subtle italic py-4 text-center flex flex-col items-center gap-2">
              <div className="w-8 h-8 rounded-full bg-sc-fg-subtle/10 flex items-center justify-center">
                <Code width={16} height={16} className="text-sc-fg-subtle/50" />
              </div>
              No tool calls recorded
            </div>
          )}

          {/* Show final result summary if present */}
          {taskResult && (
            <ResultSummary
              taskResult={taskResult}
              isError={isError}
              durationMs={durationMs}
              costUsd={costUsd}
            />
          )}
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// ParallelAgentsBlock
// =============================================================================

/** Container for multiple subagents spawned in parallel */
export function ParallelAgentsBlock({ subagents, resultsByToolId }: ParallelAgentsBlockProps) {
  const workingCount = subagents.filter(s => !s.taskResult).length;
  const completedCount = subagents.length - workingCount;
  const allDone = workingCount === 0;

  return (
    <div
      className={`rounded-lg border overflow-hidden animate-slide-up transition-all duration-300 ${
        allDone
          ? 'border-sc-green/20 bg-gradient-to-r from-sc-green/5 to-sc-cyan/5'
          : 'border-sc-cyan/30 bg-gradient-to-r from-sc-cyan/5 via-sc-purple/5 to-sc-cyan/5'
      }`}
    >
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-sc-cyan/10">
        {!allDone ? (
          <Spinner size="sm" className="text-sc-cyan shrink-0" />
        ) : (
          <div className="animate-bounce-in">
            <Check width={14} height={14} className="text-sc-green shrink-0" />
          </div>
        )}
        <span className={`text-xs font-medium ${allDone ? 'text-sc-green' : 'text-sc-cyan'}`}>
          Parallel Agents ({subagents.length})
        </span>
        <span className="text-[10px] text-sc-fg-subtle flex-1">
          {allDone ? (
            <span className="text-sc-green animate-fade-in">All complete</span>
          ) : (
            <>
              <span className="text-sc-yellow animate-pulse">{workingCount} working</span>
              {completedCount > 0 && (
                <span className="text-sc-green ml-2 animate-fade-in">{completedCount} done</span>
              )}
            </>
          )}
        </span>
      </div>

      {/* Individual agents */}
      <div className="p-2 space-y-1.5">
        {subagents.map(subagent => (
          <SubagentBlock
            key={subagent.taskCall.id}
            taskCall={subagent.taskCall}
            taskResult={subagent.taskResult}
            nestedCalls={subagent.nestedCalls}
            resultsByToolId={resultsByToolId}
          />
        ))}
      </div>
    </div>
  );
}

// =============================================================================
// Helper Components
// =============================================================================

interface StatusIconProps {
  isBackground?: boolean;
  pollStatus?: 'running' | 'completed' | 'failed';
  isWorking: boolean;
  isError?: boolean;
}

function StatusIcon({ isBackground, pollStatus, isWorking, isError }: StatusIconProps) {
  if (isBackground && pollStatus) {
    if (pollStatus === 'running') {
      return <Spinner size="sm" className="text-sc-cyan shrink-0" />;
    }
    if (pollStatus === 'failed') {
      return <Xmark width={14} height={14} className="text-sc-red shrink-0" />;
    }
    return (
      <div className="animate-bounce-in">
        <Check width={14} height={14} className="text-sc-green shrink-0" />
      </div>
    );
  }

  if (isWorking) {
    return <Spinner size="sm" className="text-sc-purple shrink-0" />;
  }

  if (isError) {
    return <Xmark width={14} height={14} className="text-sc-red shrink-0 animate-wiggle" />;
  }

  return (
    <div className="animate-bounce-in">
      <Check width={14} height={14} className="text-sc-green shrink-0" />
    </div>
  );
}

function StatusLabel({ isBackground, pollStatus, isWorking, isError }: StatusIconProps) {
  if (isBackground && pollStatus) {
    if (pollStatus === 'running') {
      return <span className="text-[10px] text-sc-yellow font-medium animate-pulse">running</span>;
    }
    if (pollStatus === 'failed') {
      return <span className="text-[10px] text-sc-red font-medium">failed</span>;
    }
    return <span className="text-[10px] text-sc-green font-medium animate-fade-in">done</span>;
  }

  if (isWorking) {
    return <span className="text-[10px] text-sc-yellow font-medium animate-pulse">working</span>;
  }

  if (isError) {
    return <span className="text-[10px] text-sc-red font-medium">failed</span>;
  }

  return <span className="text-[10px] text-sc-green font-medium animate-fade-in">done</span>;
}

interface LivePreviewProps {
  calls: ChatMessage[];
  resultsByToolId: Map<string, ChatMessage>;
}

import { forwardRef } from 'react';

const LivePreview = forwardRef<HTMLDivElement, LivePreviewProps>(
  ({ calls, resultsByToolId }, ref) => (
    <div
      ref={ref}
      className="px-3 pb-2 space-y-0.5 border-t border-sc-purple/10 max-h-20 overflow-y-auto"
    >
      {calls.map(call => {
        const pairedResult = resultsByToolId.get(call.metadata?.tool_id as string);
        const iconName = call.metadata?.icon as string | undefined;
        const toolName = call.metadata?.tool_name as string | undefined;
        const Icon = getToolIcon(iconName);
        const hasCallResult = !!pairedResult;
        const callError = pairedResult?.metadata?.is_error as boolean | undefined;
        const statusClass = !hasCallResult
          ? 'text-sc-purple'
          : callError
            ? 'text-sc-red'
            : 'text-sc-green';

        return (
          <div
            key={call.id}
            className="flex items-center gap-1.5 text-[10px] font-mono py-0.5 animate-slide-up"
          >
            {!hasCallResult ? (
              <Spinner size="sm" className="text-sc-purple" />
            ) : (
              <Icon width={10} height={10} className={statusClass} />
            )}
            <span className={statusClass}>{toolName}</span>
            <span className="text-sc-fg-subtle truncate">{call.content.slice(0, 40)}</span>
          </div>
        );
      })}
    </div>
  )
);
LivePreview.displayName = 'LivePreview';

interface ResultSummaryProps {
  taskResult: ChatMessage;
  isError?: boolean;
  durationMs?: number;
  costUsd?: number;
}

function ResultSummary({ taskResult, isError, durationMs, costUsd }: ResultSummaryProps) {
  return (
    <div
      className={`mt-2 p-3 rounded-lg text-xs animate-fade-in ${
        isError
          ? 'bg-sc-red/10 border border-sc-red/20 text-sc-red'
          : 'bg-sc-green/10 border border-sc-green/20 text-sc-green'
      }`}
    >
      <div className="flex items-center justify-between">
        <span className="font-medium flex items-center gap-1.5">
          {isError ? (
            <>
              <Xmark width={12} height={12} /> Failed
            </>
          ) : (
            <>
              <Check width={12} height={12} /> Completed
            </>
          )}
        </span>
        <div className="flex items-center gap-2 text-[10px] text-sc-fg-subtle">
          {durationMs && <span>{formatDuration(durationMs)}</span>}
          {costUsd && costUsd > 0 && <span>${costUsd.toFixed(4)}</span>}
        </div>
      </div>
      {taskResult.content && taskResult.content.length < 300 && (
        <p className="text-sc-fg-muted mt-2 text-[11px] leading-relaxed">{taskResult.content}</p>
      )}
    </div>
  );
}
